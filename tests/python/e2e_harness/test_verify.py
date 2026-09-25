"""Unit tests for tests/e2e/verify.py.

All fixtures under tests/python/e2e_harness/fixtures/ are synthetic data
constructed for these tests, not captured from a real run (each fixture file
carries a "_synthetic": true marker documenting this). No network calls; no
real credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_pr_review.vcs.marker import build_summary_marker
from tests.e2e.config import ExpectedFinding
from tests.e2e.platforms import RawEvidence
from tests.e2e.run_e2e import validate_platforms
from tests.e2e.verify import (
    InfraFailure,
    load_telemetry,
    parse_review_log_line,
    verify_analyzer_findings,
    verify_event_not_degraded,
    verify_model,
    verify_posting_surfaces,
    verify_summary_marker,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- parse_review_log_line ------------------------------------------------

def test_parse_review_log_line_complete():
    result = parse_review_log_line(
        "some noise\nReview complete: 4 findings, 0 failed agents, event=APPROVE, base=main\nmore noise"
    )
    assert not result.skipped
    assert result.findings_count == 4
    assert result.failed_agents == 0
    assert result.event == "APPROVE"
    assert result.base == "main"


def test_parse_review_log_line_skipped():
    result = parse_review_log_line("Review skipped: cost ceiling exceeded")
    assert result.skipped
    assert result.skip_reason == "cost ceiling exceeded"


def test_parse_review_log_line_neither_raises_infra_failure():
    with pytest.raises(InfraFailure):
        parse_review_log_line("container crashed with no recognizable output")


# --- load_telemetry --------------------------------------------------------

def test_load_telemetry_happy_path():
    data = load_telemetry(FIXTURES_DIR / "telemetry_valid.json")
    assert data["outcome"] == "APPROVE"
    assert data["model_standard"] == "claude-sonnet-5"


def test_load_telemetry_missing_file_is_infra_failure(tmp_path):
    with pytest.raises(InfraFailure):
        load_telemetry(tmp_path / "does-not-exist.json")


def test_load_telemetry_malformed_json_is_infra_failure():
    with pytest.raises(InfraFailure):
        load_telemetry(FIXTURES_DIR / "telemetry_malformed.json")


def test_load_telemetry_missing_required_field_is_infra_failure():
    with pytest.raises(InfraFailure):
        load_telemetry(FIXTURES_DIR / "telemetry_missing_field.json")


def test_load_telemetry_empty_file_is_infra_failure(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(InfraFailure):
        load_telemetry(empty)


# --- verify_summary_marker --------------------------------------------------

def test_verify_summary_marker_correct_sha_github_style():
    # Built via the product's own build_summary_marker (not a hand-rolled
    # string) so this test can't drift from the real format the way an
    # earlier version of this suite did -- see verify_summary_marker's
    # docstring for that history.
    marker = build_summary_marker("abc1234")
    evidence = RawEvidence(summary_body=f"hello\n{marker}\nworld")
    verdict = verify_summary_marker(evidence, "abc1234def")
    assert verdict.ok


def test_verify_summary_marker_bitbucket_hidden_form():
    marker = build_summary_marker("abc1234", hidden=True)
    evidence = RawEvidence(summary_body=f"visible text\n{marker}\nmore text")
    verdict = verify_summary_marker(evidence, "abc1234def")
    assert verdict.ok


def test_verify_summary_marker_stale_sha_fails():
    marker = build_summary_marker("deadbeef")
    evidence = RawEvidence(summary_body=marker)
    verdict = verify_summary_marker(evidence, "abc1234def")
    assert not verdict.ok
    assert "stale" in verdict.reason.lower() or "does not match" in verdict.reason.lower()


def test_verify_summary_marker_missing_marker_fails():
    evidence = RawEvidence(summary_body="no marker here at all")
    verdict = verify_summary_marker(evidence, "abc1234")
    assert not verdict.ok


# --- verify_event_not_degraded ---------------------------------------------

def test_verify_event_not_degraded_ok_via_posted_event():
    # telemetry.outcome is verified (against cli.py/orchestrate.py) to be
    # the pre-posting decision, never updated to reflect a post-time
    # degrade -- so it must NOT be consulted here. Passing a telemetry
    # fixture alongside a matching posted_event exercises that telemetry is
    # correctly ignored, not that it drives the verdict.
    telemetry = load_telemetry(FIXTURES_DIR / "telemetry_valid.json")
    verdict = verify_event_not_degraded("APPROVE", "APPROVE", telemetry=telemetry)
    assert verdict.ok
    assert "posted_event" in verdict.reason


def test_verify_event_not_degraded_caught_via_posted_event():
    # The real #651 degrade: the run intended APPROVE (telemetry.outcome
    # would show "APPROVE", the pre-posting decision) but the container
    # actually posted a plain COMMENT -- only posted_event (parsed from the
    # "Review complete: ... event=..." log line) carries that signal.
    telemetry = load_telemetry(FIXTURES_DIR / "telemetry_valid.json")
    verdict = verify_event_not_degraded("COMMENT", "APPROVE", telemetry=telemetry)
    assert not verdict.ok
    assert "posted_event" in verdict.reason


def test_verify_event_not_degraded_caught_via_text_fallback():
    # Only reached when posted_event is unavailable (empty) -- posted_event
    # takes priority whenever it's present, per the function's docstring.
    body = "This PR has NOT been approved because a prior review is unresolved."
    verdict = verify_event_not_degraded("", "APPROVE", telemetry=None, summary_body=body)
    assert not verdict.ok
    assert "summary body" in verdict.reason


def test_verify_event_not_degraded_unavailable_and_no_fallback_text_fails():
    verdict = verify_event_not_degraded("", "APPROVE", telemetry=None, summary_body="nothing relevant here")
    assert not verdict.ok
    assert "unavailable" in verdict.reason


def test_verify_event_not_degraded_not_applicable_for_comment_expectation():
    verdict = verify_event_not_degraded("COMMENT", "COMMENT")
    assert verdict.ok


# --- verify_model ------------------------------------------------------------

def test_verify_model_matches_standard():
    telemetry = load_telemetry(FIXTURES_DIR / "telemetry_valid.json")
    verdict = verify_model(telemetry, "claude-sonnet-5")
    assert verdict.ok


def test_verify_model_mismatch_fails():
    telemetry = load_telemetry(FIXTURES_DIR / "telemetry_valid.json")
    verdict = verify_model(telemetry, "gpt-5.4")
    assert not verdict.ok


# --- verify_posting_surfaces -------------------------------------------------

def test_verify_posting_surfaces_all_present():
    evidence = RawEvidence(
        summary_body="the summary",
        inline_comments=[{"body": "an inline finding"}],
        annotations=[{"summary": "an annotation"}],
    )
    verdicts = verify_posting_surfaces(evidence, bitbucket=True)
    assert all(v.ok for v in verdicts)
    names = {v.name for v in verdicts}
    assert names == {"posting_summary", "posting_inline", "posting_annotations"}


def test_verify_posting_surfaces_missing_summary_fails():
    evidence = RawEvidence(summary_body="", inline_comments=[{"body": "x"}])
    verdicts = verify_posting_surfaces(evidence)
    by_name = {v.name: v for v in verdicts}
    assert not by_name["posting_summary"].ok
    assert by_name["posting_inline"].ok


def test_verify_posting_surfaces_no_bitbucket_annotation_check_for_other_platforms():
    evidence = RawEvidence(summary_body="x", inline_comments=[{"body": "y"}])
    verdicts = verify_posting_surfaces(evidence, bitbucket=False)
    names = {v.name for v in verdicts}
    assert "posting_annotations" not in names


def test_verify_posting_surfaces_truncated_flag_adds_pagination_note_not_failure():
    evidence = RawEvidence(summary_body="x", inline_comments=[{"body": "y"}], truncated=True)
    verdicts = verify_posting_surfaces(evidence)
    for v in verdicts:
        assert v.ok  # truncation is a warning, not a failure
        assert v.pagination_note is not None


# --- verify_analyzer_findings -------------------------------------------------

def test_verify_analyzer_findings_happy_path():
    evidence = RawEvidence(
        summary_body="finding in docs/index.md category=documentation",
        inline_comments=[{"body": "a"}, {"body": "b"}, {"body": "c"}],
    )
    expected = [ExpectedFinding(path_substring="docs/", category="documentation")]
    verdict = verify_analyzer_findings(evidence, expected, findings_floor=3)
    assert verdict.ok


def test_verify_analyzer_findings_below_floor_fails():
    evidence = RawEvidence(summary_body="finding in docs/ category=documentation", inline_comments=[])
    expected = [ExpectedFinding(path_substring="docs/", category="documentation")]
    verdict = verify_analyzer_findings(evidence, expected, findings_floor=3)
    assert not verdict.ok
    assert "floor" in verdict.reason


def test_verify_analyzer_findings_missing_expected_fails():
    evidence = RawEvidence(
        summary_body="unrelated content",
        inline_comments=[{"body": "a"}, {"body": "b"}, {"body": "c"}],
    )
    expected = [ExpectedFinding(path_substring="docs/", category="documentation")]
    verdict = verify_analyzer_findings(evidence, expected, findings_floor=3)
    assert not verdict.ok


# --- validate_platforms (plan's offline validation) --------------------------

def test_validate_platforms_happy_path():
    assert validate_platforms(("github", "gitlab", "bitbucket")) == ["bitbucket", "github", "gitlab"]


def test_validate_platforms_empty_raises():
    with pytest.raises(ValueError, match="no platforms"):
        validate_platforms(())


def test_validate_platforms_unknown_raises():
    with pytest.raises(ValueError, match="unknown platform"):
        validate_platforms(("github", "not-a-platform"))


def test_validate_platforms_duplicate_raises():
    with pytest.raises(ValueError, match="duplicate platform"):
        validate_platforms(("github", "github"))


def test_validate_platforms_release_profile_requires_exact_set():
    with pytest.raises(ValueError, match="release"):
        validate_platforms(("github", "gitlab"), profile="release")


def test_validate_platforms_release_profile_happy_path():
    result = validate_platforms(("github", "gitlab", "bitbucket"), profile="release")
    assert result == ["bitbucket", "github", "gitlab"]
