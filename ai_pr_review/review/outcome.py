"""Single-source review outcome classifier.

Replaces the three duplicated risk-classification code paths in
post-review.sh, post-review-gitlab.sh, post-review-bitbucket.sh, and
vcs/common.sh::classify_risk. Resolves #181 and #192.

Critical policy change from bash: any failed finding-producing agent forces
may_approve=False and incomplete=True. When this overrides an APPROVE-eligible
severity (Medium/Low), the event downgrades to COMMENT. Critical/High remain
REQUEST_CHANGES (they were never going to approve anyway).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Risk = Literal["None", "Low", "Medium", "High", "Critical", "Unknown"]
ReviewEvent = Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"]


class _FindingLike(Protocol):
    """Anything with a `severity: str` attribute satisfies this."""

    severity: str


@dataclass(frozen=True)
class ReviewOutcome:
    risk: Risk
    event: ReviewEvent
    may_approve: bool
    incomplete: bool
    finding_total: int


def _has_severity(findings: Sequence[_FindingLike], target: str) -> bool:
    target_lower = target.lower()
    return any(f.severity.lower() == target_lower for f in findings)


def classify_review_outcome(
    findings: Sequence[_FindingLike],
    failed_agents: Sequence[str],
    mode: str,  # noqa: ARG001 — reserved for future policy (quick/security-only)
) -> ReviewOutcome:
    """Classify a review outcome from findings and failed-agent tracking.

    Args:
        findings: Sequence of findings; only `.severity` is inspected.
        failed_agents: Names of agents that failed during dispatch.
        mode: Review mode (currently logging-only; reserved).
    """
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
