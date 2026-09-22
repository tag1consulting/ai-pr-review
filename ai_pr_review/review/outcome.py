"""Single-source review outcome classifier.

Replaces the three duplicated risk-classification code paths in
post-review.sh, post-review-gitlab.sh, post-review-bitbucket.sh, and
vcs/common.sh::classify_risk. Resolves #181 and #192.

Critical policy change from bash: any failed finding-producing agent forces
may_approve=False and incomplete=True. When this overrides an APPROVE-eligible
severity (Medium/Low), the event downgrades to COMMENT. Critical/High remain
REQUEST_CHANGES (they were never going to approve anyway).

``cap_review_outcome`` (#858) is a second, orthogonal stage applied *after*
``classify_review_outcome``: it clamps the classifier's *event* to at most
what a configured ``approval_ceiling`` permits, without touching *risk*,
*may_approve*, *incomplete*, or *finding_total*. Kept as a separate function
rather than folded into the classifier itself so the classifier stays a
pure function of findings/failed-agents (independently testable, and its
own test suite untouched), and so ``may_approve`` keeps meaning "the
severity verdict" rather than "what actually got posted" -- see
``ReviewOutcome.may_approve``'s docstring for why that split matters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Risk = Literal["None", "Low", "Medium", "High", "Critical", "Unknown"]
ReviewEvent = Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"]
ReviewMode = Literal["full", "quick", "summary-only", "security-only"]
ApprovalCeiling = Literal["approve", "request-changes", "comment"]

_VALID_MODES: frozenset[str] = frozenset({"full", "quick", "summary-only", "security-only"})
_VALID_CEILINGS: frozenset[str] = frozenset({"approve", "request-changes", "comment"})

# Which events each ceiling tier permits the classifier's *event* to pass
# through unchanged. Anything not in the set is downgraded to COMMENT --
# never re-mapped between APPROVE and REQUEST_CHANGES, since a clean PR
# and a Critical-finding PR are not the same situation just because both
# are disallowed under a stricter ceiling.
_CEILING_ALLOWS: dict[ApprovalCeiling, frozenset[ReviewEvent]] = {
    "approve": frozenset({"APPROVE", "REQUEST_CHANGES", "COMMENT"}),
    "request-changes": frozenset({"REQUEST_CHANGES", "COMMENT"}),
    "comment": frozenset({"COMMENT"}),
}


class _FindingLike(Protocol):
    """Anything with a `severity: str` attribute satisfies this."""

    severity: str


@dataclass(frozen=True)
class ReviewOutcome:
    risk: Risk
    event: ReviewEvent
    may_approve: bool
    """The classifier's severity verdict: True iff findings/failed-agents
    alone would have earned an APPROVE. This is independent of any
    ``approval_ceiling`` (#858) applied afterward by ``cap_review_outcome``
    -- capping changes *event* only, never *may_approve*, so
    ``AI_FAIL_ON_FINDINGS`` (which reads ``may_approve``, not ``event``)
    keeps its exit code unaffected by the ceiling. See
    ``cap_review_outcome``'s docstring."""
    incomplete: bool
    finding_total: int


def _has_severity(findings: Sequence[_FindingLike], target: str) -> bool:
    target_lower = target.lower()
    return any(f.severity.lower() == target_lower for f in findings)


def classify_review_outcome(
    findings: Sequence[_FindingLike],
    failed_agents: Sequence[str],
    mode: ReviewMode,
) -> ReviewOutcome:
    """Classify a review outcome from findings and failed-agent tracking.

    This function does *not* apply ``approval_ceiling`` (#858) -- both
    production call sites (``orchestrate.py``) must pass this result
    through ``cap_review_outcome`` themselves before it reaches a VCS
    provider. A new call site that skips that step would silently emit a
    real APPROVE regardless of a configured ceiling.

    Args:
        findings: Sequence of findings; only `.severity` is inspected.
        failed_agents: Names of agents that failed during dispatch.
        mode: Review mode (currently logging-only; reserved for future policy
            like quick-mode approval thresholds). Must be one of the ReviewMode
            literal values; passing another string raises ValueError.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid review mode {mode!r}; must be one of {sorted(_VALID_MODES)}"
        )
    finding_total = len(findings)
    any_failed = len(failed_agents) > 0

    if finding_total == 0:
        if any_failed:
            return ReviewOutcome(
                risk="Unknown",
                event="COMMENT",
                may_approve=False,
                incomplete=True,
                finding_total=0,
            )
        return ReviewOutcome(
            risk="None",
            event="APPROVE",
            may_approve=True,
            incomplete=False,
            finding_total=0,
        )

    if _has_severity(findings, "Critical"):
        risk: Risk = "Critical"
        event: ReviewEvent = "REQUEST_CHANGES"
    elif _has_severity(findings, "High"):
        risk = "High"
        event = "REQUEST_CHANGES"
    elif _has_severity(findings, "Medium"):
        risk = "Medium"
        event = "APPROVE"
    else:
        risk = "Low"
        event = "APPROVE"

    if any_failed and event == "APPROVE":
        event = "COMMENT"

    may_approve = event == "APPROVE"
    incomplete = any_failed

    return ReviewOutcome(
        risk=risk,
        event=event,
        may_approve=may_approve,
        incomplete=incomplete,
        finding_total=finding_total,
    )


def normalize_approval_ceiling(raw: str) -> ApprovalCeiling:
    """Normalize and validate a raw ``AI_APPROVAL_CEILING``/``approval-ceiling``
    value.

    Accepts case-insensitively and with either hyphens or underscores (so a
    value copied from a log line's ``REQUEST_CHANGES`` event name still
    works), e.g. ``"REQUEST_CHANGES"``, ``"request_changes"``, and
    ``"Request-Changes"`` all normalize to ``"request-changes"``.

    Raises ``ValueError`` on anything else -- deliberately not a
    warn-and-default fallback like some other tolerant env-var parsing in
    this codebase, because silently falling back to the permissive
    ``"approve"`` default on a typo is exactly the failure this feature
    exists to prevent.
    """
    normalized = raw.strip().lower().replace("_", "-")
    if normalized not in _VALID_CEILINGS:
        raise ValueError(
            f"Invalid approval ceiling {raw!r}; must be one of {sorted(_VALID_CEILINGS)}"
        )
    return normalized  # type: ignore[return-value]  # narrowed by the membership check above


def cap_review_outcome(outcome: ReviewOutcome, ceiling: ApprovalCeiling) -> ReviewOutcome:
    """Clamp *outcome*'s ``event`` to at most what *ceiling* permits (#858).

    ``ceiling="approve"`` (the default) is the identity transform --
    behavior is completely unchanged from before this feature existed.
    ``ceiling="request-changes"`` downgrades an ``APPROVE`` to ``COMMENT``
    but leaves ``REQUEST_CHANGES`` untouched. ``ceiling="comment"``
    downgrades both ``APPROVE`` and ``REQUEST_CHANGES`` to ``COMMENT``, so
    the bot never sets any formal review state. An event is never re-mapped
    between ``APPROVE`` and ``REQUEST_CHANGES`` in either direction -- both
    disallowed cases become ``COMMENT``, since a clean PR and a
    Critical-finding PR are different situations that happen to both be
    disallowed, not equivalent to each other.

    Only ``event`` changes. ``risk``, ``may_approve``, ``incomplete``, and
    ``finding_total`` are carried through unchanged -- see
    ``ReviewOutcome.may_approve``'s docstring for why that split is
    load-bearing for ``AI_FAIL_ON_FINDINGS``.
    """
    if outcome.event in _CEILING_ALLOWS[ceiling]:
        return outcome
    return ReviewOutcome(
        risk=outcome.risk,
        event="COMMENT",
        may_approve=outcome.may_approve,
        incomplete=outcome.incomplete,
        finding_total=outcome.finding_total,
    )
