"""Tests for ai_pr_review.review.outcome.classify_review_outcome."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_pr_review.review.outcome import (
    ReviewOutcome,
    cap_review_outcome,
    classify_review_outcome,
    normalize_approval_ceiling,
)


@dataclass
class _F:
    """Minimal test finding stand-in (satisfies the Finding protocol)."""

    severity: str


def _critical() -> _F:
    return _F(severity="Critical")


def _high() -> _F:
    return _F(severity="High")


def _medium() -> _F:
    return _F(severity="Medium")


def _low() -> _F:
    return _F(severity="Low")


# ---------------------------------------------------------------------------
# Zero findings
# ---------------------------------------------------------------------------

def test_empty_findings_no_failures_approves() -> None:
    outcome = classify_review_outcome([], [], mode="full")
    assert outcome.risk == "None"
    assert outcome.event == "APPROVE"
    assert outcome.may_approve is True
    assert outcome.incomplete is False
    assert outcome.finding_total == 0


def test_empty_findings_with_failures_comments() -> None:
    outcome = classify_review_outcome([], ["security-reviewer"], mode="full")
    assert outcome.risk == "Unknown"
    assert outcome.event == "COMMENT"
    assert outcome.may_approve is False
    assert outcome.incomplete is True


# ---------------------------------------------------------------------------
# Severity-driven events
# ---------------------------------------------------------------------------

def test_critical_requests_changes() -> None:
    outcome = classify_review_outcome([_critical(), _low()], [], mode="full")
    assert outcome.risk == "Critical"
    assert outcome.event == "REQUEST_CHANGES"
    assert outcome.may_approve is False
    assert outcome.incomplete is False
    assert outcome.finding_total == 2


def test_high_requests_changes() -> None:
    outcome = classify_review_outcome([_high(), _medium(), _low()], [], mode="full")
    assert outcome.risk == "High"
    assert outcome.event == "REQUEST_CHANGES"
    assert outcome.may_approve is False


def test_medium_only_approves() -> None:
    outcome = classify_review_outcome([_medium(), _medium()], [], mode="full")
    assert outcome.risk == "Medium"
    assert outcome.event == "APPROVE"
    assert outcome.may_approve is True
    assert outcome.incomplete is False


def test_low_only_approves() -> None:
    outcome = classify_review_outcome([_low()], [], mode="full")
    assert outcome.risk == "Low"
    assert outcome.event == "APPROVE"
    assert outcome.may_approve is True


# ---------------------------------------------------------------------------
# Failed-agent policy — 2.FR-6 core contract
# ---------------------------------------------------------------------------

def test_medium_with_failed_agent_downgrades_to_comment() -> None:
    # BUG WE'RE FIXING: bash used to APPROVE here. Now we MUST NOT.
    outcome = classify_review_outcome([_medium()], ["silent-failure-hunter"], mode="full")
    assert outcome.risk == "Medium"
    assert outcome.event == "COMMENT"
    assert outcome.may_approve is False
    assert outcome.incomplete is True


def test_low_with_failed_agent_downgrades_to_comment() -> None:
    outcome = classify_review_outcome([_low()], ["code-reviewer"], mode="full")
    assert outcome.event == "COMMENT"
    assert outcome.may_approve is False
    assert outcome.incomplete is True


def test_critical_with_failed_agent_still_requests_changes() -> None:
    outcome = classify_review_outcome(
        [_critical()], ["security-reviewer"], mode="full"
    )
    assert outcome.risk == "Critical"
    assert outcome.event == "REQUEST_CHANGES"
    assert outcome.may_approve is False
    assert outcome.incomplete is True


def test_high_with_failed_agent_still_requests_changes() -> None:
    outcome = classify_review_outcome([_high()], ["edge-case-hunter"], mode="full")
    assert outcome.event == "REQUEST_CHANGES"
    assert outcome.may_approve is False
    assert outcome.incomplete is True


def test_multiple_failed_agents_aggregate() -> None:
    outcome = classify_review_outcome(
        [], ["a", "b", "c"], mode="full"
    )
    assert outcome.incomplete is True
    assert outcome.may_approve is False


# ---------------------------------------------------------------------------
# Case-insensitive severity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sev", ["critical", "CRITICAL", "Critical", "CrItIcAl"])
def test_severity_case_insensitive_critical(sev: str) -> None:
    outcome = classify_review_outcome([_F(severity=sev)], [], mode="full")
    assert outcome.risk == "Critical"
    assert outcome.event == "REQUEST_CHANGES"


@pytest.mark.parametrize("sev", ["high", "HIGH", "High"])
def test_severity_case_insensitive_high(sev: str) -> None:
    outcome = classify_review_outcome([_F(severity=sev)], [], mode="full")
    assert outcome.risk == "High"


# ---------------------------------------------------------------------------
# Unknown severity handling
# ---------------------------------------------------------------------------

def test_unknown_severity_does_not_escalate() -> None:
    # finding_total still counts, but risk stays Low (only recognised severities gate)
    outcome = classify_review_outcome(
        [_F(severity="info"), _F(severity="warning")], [], mode="full"
    )
    assert outcome.finding_total == 2
    # No recognised severity → treated like "Low" since findings exist but none
    # match Critical/High/Medium. This mirrors the bash fall-through into `else`.
    assert outcome.risk == "Low"
    assert outcome.event == "APPROVE"


def test_unknown_severity_mixed_with_critical_still_critical() -> None:
    outcome = classify_review_outcome(
        [_F(severity="info"), _critical()], [], mode="full"
    )
    assert outcome.risk == "Critical"


# ---------------------------------------------------------------------------
# Mode is accepted but not yet policy-active
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["full", "quick", "summary-only", "security-only"])
def test_mode_passes_through(mode: str) -> None:
    outcome = classify_review_outcome([_low()], [], mode=mode)  # type: ignore[arg-type]
    assert outcome.event == "APPROVE"


@pytest.mark.parametrize("bad_mode", ["", "FULL", "quck", "nope", "None"])
def test_invalid_mode_raises(bad_mode: str) -> None:
    with pytest.raises(ValueError, match="Invalid review mode"):
        classify_review_outcome([], [], mode=bad_mode)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ReviewOutcome is frozen
# ---------------------------------------------------------------------------

def test_review_outcome_is_frozen() -> None:
    outcome = classify_review_outcome([], [], mode="full")
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        outcome.risk = "Critical"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Approval ceiling (#858)
# ---------------------------------------------------------------------------

_ALL_EVENTS = ["APPROVE", "REQUEST_CHANGES", "COMMENT"]


def _outcome(event: str) -> ReviewOutcome:
    return ReviewOutcome(
        risk="Low", event=event, may_approve=(event == "APPROVE"),  # type: ignore[arg-type]
        incomplete=False, finding_total=1,
    )


@pytest.mark.parametrize("event", _ALL_EVENTS)
def test_approve_ceiling_is_identity(event: str) -> None:
    outcome = _outcome(event)
    assert cap_review_outcome(outcome, "approve") is outcome


def test_request_changes_ceiling_downgrades_approve_to_comment() -> None:
    capped = cap_review_outcome(_outcome("APPROVE"), "request-changes")
    assert capped.event == "COMMENT"


def test_request_changes_ceiling_preserves_request_changes() -> None:
    outcome = _outcome("REQUEST_CHANGES")
    assert cap_review_outcome(outcome, "request-changes") is outcome


def test_request_changes_ceiling_preserves_comment() -> None:
    outcome = _outcome("COMMENT")
    assert cap_review_outcome(outcome, "request-changes") is outcome


def test_comment_ceiling_downgrades_approve_to_comment() -> None:
    capped = cap_review_outcome(_outcome("APPROVE"), "comment")
    assert capped.event == "COMMENT"


def test_comment_ceiling_downgrades_request_changes_to_comment() -> None:
    capped = cap_review_outcome(_outcome("REQUEST_CHANGES"), "comment")
    assert capped.event == "COMMENT"


def test_comment_ceiling_preserves_comment() -> None:
    outcome = _outcome("COMMENT")
    assert cap_review_outcome(outcome, "comment") is outcome


@pytest.mark.parametrize(
    "event,ceiling",
    [("APPROVE", "request-changes"), ("APPROVE", "comment"), ("REQUEST_CHANGES", "comment")],
)
def test_cap_preserves_risk_may_approve_incomplete_and_total(event: str, ceiling: str) -> None:
    """Load-bearing for the AI_FAIL_ON_FINDINGS decision (#858): capping
    must change *event* only. may_approve in particular must reflect the
    classifier's severity verdict regardless of what actually gets posted,
    or a ceiling would silently corrupt the fail-on-findings exit code."""
    original = ReviewOutcome(
        risk="Critical", event=event, may_approve=False,  # type: ignore[arg-type]
        incomplete=True, finding_total=3,
    )
    capped = cap_review_outcome(original, ceiling)  # type: ignore[arg-type]
    assert capped.risk == original.risk
    assert capped.may_approve == original.may_approve
    assert capped.incomplete == original.incomplete
    assert capped.finding_total == original.finding_total


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("approve", "approve"),
        ("APPROVE", "approve"),
        ("request-changes", "request-changes"),
        ("REQUEST_CHANGES", "request-changes"),
        ("request_changes", "request-changes"),
        ("Request-Changes", "request-changes"),
        ("comment", "comment"),
        ("  comment  ".strip(), "comment"),
    ],
)
def test_normalize_approval_ceiling_accepts_known_forms(raw: str, expected: str) -> None:
    assert normalize_approval_ceiling(raw) == expected


@pytest.mark.parametrize("bad", ["", "approved", "block", "REQUESTCHANGES", "none"])
def test_normalize_approval_ceiling_rejects_unknown_value(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid approval ceiling"):
        normalize_approval_ceiling(bad)
