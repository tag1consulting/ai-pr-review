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

    def resolve_stale(self) -> StaleResult:
        """Resolve/dismiss stale threads or reviews — marker-gated."""
        ...

    def post_skip_comment(self, reason: str) -> SummaryResult:
        """Post a no-op PR comment on skip paths (marker-bearing)."""
        ...
