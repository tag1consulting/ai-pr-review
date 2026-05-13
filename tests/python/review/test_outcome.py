"""Tests for ai_pr_review.review.outcome.classify_review_outcome."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_pr_review.review.outcome import classify_review_outcome


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
    outcome = classify_review_outcome([_low()], [], mode=mode)
    assert outcome.event == "APPROVE"


# ---------------------------------------------------------------------------
# ReviewOutcome is frozen
# ---------------------------------------------------------------------------

def test_review_outcome_is_frozen() -> None:
    outcome = classify_review_outcome([], [], mode="full")
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        outcome.risk = "Critical"  # type: ignore[misc]
