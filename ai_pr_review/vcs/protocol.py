"""VCS provider protocol and shared result dataclasses.

Every concrete provider (GitHub, GitLab, Bitbucket) implements this Protocol.
Tests supply fakes without inheritance ceremony.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from ai_pr_review.findings.models import Finding

PostEvent = Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"]


@dataclass(frozen=True)
class DiffContext:
    """Minimal diff information a provider needs to validate inline anchors."""

    diff_text: str
    head_sha: str


@dataclass(frozen=True)
class SummaryResult:
    """Outcome of `post_summary`."""

    comment_id: int | None
    created: bool
    updated: bool
    error: str | None = None

    def __post_init__(self) -> None:
        """Auto-populate error when the API returned no ID after a create/update.

        Invariant: if created or updated is True, comment_id must be non-None.
        When a provider constructs SummaryResult(comment_id=None, created=True, ...)
        it means the POST/PATCH call succeeded at the HTTP level but returned no
        usable ID (e.g. id=0).  Callers get a programmatic signal via error rather
        than the ambiguous ok=False/error=None state.

        Providers that know the HTTP status or response body can pass a richer
        error string directly; this sentinel fires only when error is still None.
        """
        if (
            self.error is None
            and self.comment_id is None
            and (self.created or self.updated)
        ):
            object.__setattr__(
                self, "error", "API returned no comment ID after create/update"
            )

    @property
    def ok(self) -> bool:
        # ok if no error AND either we have a real comment ID (post succeeded)
        # or nothing was posted (no-op / skip path where id=0 is acceptable).
        return self.error is None and (
            self.comment_id is not None or (not self.created and not self.updated)
        )


@dataclass(frozen=True)
class FindingsResult:
    """Outcome of `post_findings`."""

    review_id: int | None
    inline_posted: int
    body_findings: int
    event: PostEvent
    degraded_to_comment: bool = False
    error: str | None = None
    # Canonical-review-reuse counters (GitHub only; always 0/False on
    # GitLab/Bitbucket, which don't implement the reuse classification).
    # inline_updated: comments PATCHed in place for an update/escalate
    # classification. suppressed: findings matching a durable "dismissed"
    # verdict, never reposted. replies_posted: escalation/recurrence
    # notification replies posted on existing threads. reused_review: True
    # when this call PUT the canonical review's body instead of POSTing a
    # new review object.
    inline_updated: int = 0
    suppressed: int = 0
    replies_posted: int = 0
    reused_review: bool = False
    # True when reused_review is True but no write actually happened -- the
    # PR's head had already advanced past this run's diff, so a newer run
    # already owns the canonical review. Callers must not advance the SHA
    # watermark or run stale-thread cleanup keyed on this result: doing so
    # with a stale diff.head_sha could regress the incremental-diff baseline
    # backward past what the newer, actually-successful run already set.
    skipped: bool = False

    def __post_init__(self) -> None:
        """`skipped=True` only ever means "the PUT of the canonical review
        was abandoned" -- it is meaningless, and never set, on any other
        outcome. Enforcing that here (rather than trusting every future
        construction site to remember it) turns a future caller mistake
        (constructing `skipped=True` alongside a fresh POST, which would
        wrongly tell orchestrate.py's watermark/stale-cleanup logic to skip
        work that a real post actually requires) into an immediate error
        instead of a silent, hard-to-trace behavioral bug.
        """
        if self.skipped and not self.reused_review:
            raise ValueError("FindingsResult.skipped=True requires reused_review=True")

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class StaleResult:
    """Outcome of `resolve_stale`."""

    threads_resolved: int = 0
    reviews_dismissed: int = 0
    threads_skipped_no_marker: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class VcsProvider(Protocol):
    """Contract every VCS provider satisfies.

    Implementations are *synchronous* for now — posting is not on the hot path
    and a sync API keeps tests and call ordering simple. Revisit if profiling
    shows serial posting dominates.
    """

    def get_last_reviewed_sha(self) -> str | None:
        """Return the SHA embedded in the previous summary marker, or None."""
        ...

    def get_summary_body(self) -> str | None:
        """Return the current body of the summary comment, or None if not yet posted."""
        ...

    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        """Upsert the one-per-PR summary comment, keyed by SUMMARY_MARKER_PREFIX."""
        ...

    def post_findings(
        self,
        findings: Sequence[Finding],
        diff: DiffContext,
        *,
        event: PostEvent,
        failed_agents: Sequence[str] = (),
        token_table: str = "",
        agent_prompt: str = "",
        max_inline: int = 25,
        enable_suggestions: bool = True,
    ) -> FindingsResult:
        """Post findings as a PR review with inline comments where possible."""
        ...

    def resolve_stale(self, current_review_id: int | None = None) -> StaleResult:
        """Resolve/dismiss stale threads or reviews — marker-gated.

        current_review_id: the review ID posted by the current run, or None if the
        current run posted no review (e.g. degraded path). When provided and the
        review is a CHANGES_REQUESTED review, it is protected from dismissal. When
        None, all CHANGES_REQUESTED reviews are left intact as a safety guard.
        """
        ...

    def advance_sha_watermark(self, new_sha: str) -> bool:
        """Patch the sha= field in the existing summary marker without changing the body.

        Returns True if the comment was found and patched, False otherwise.
        """
        ...

    def post_skip_comment(self, reason: str) -> SummaryResult:
        """Post a no-op PR comment on skip paths (marker-bearing)."""
        ...
