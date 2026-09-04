"""Unit tests for #758's compact usage line, high-usage warning, job-log
full-table echo, and CI run-URL builder (ai_pr_review/review/reporting.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[2]


def _make_agent_result(name: str = "code-reviewer", model: str = "claude-sonnet-5") -> object:
    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage

    tl = TokenUsage(model=model, input=1000, output=500, cache_creation=0, cache_read=0)
    return AgentResult(name=name, output="", token_log=tl, truncated=False, context_tokens_used=0)


# ---------------------------------------------------------------------------
# compute_token_totals()
# ---------------------------------------------------------------------------


def test_compute_token_totals_none_on_no_data() -> None:
    from ai_pr_review.review.reporting import compute_token_totals

    assert compute_token_totals([], _REPO_ROOT) is None


def test_compute_token_totals_returns_totals() -> None:
    from ai_pr_review.review.reporting import compute_token_totals

    totals = compute_token_totals([_make_agent_result()], _REPO_ROOT)
    assert totals is not None
    assert totals.agent_count == 1
    assert totals.grand_total == 1500
    assert totals.models == ("Sonnet 5",)


# ---------------------------------------------------------------------------
# build_token_usage_line()
# ---------------------------------------------------------------------------


def test_usage_line_empty_on_no_data() -> None:
    from ai_pr_review.review.reporting import build_token_usage_line

    assert build_token_usage_line(None) == ""


def test_usage_line_shape() -> None:
    from ai_pr_review.review.reporting import build_token_usage_line, compute_token_totals

    totals = compute_token_totals([_make_agent_result()], _REPO_ROOT)
    line = build_token_usage_line(totals, run_url="https://example.test/run/1")
    assert line.startswith("_") and line.endswith("_")
    assert "Review cost:" in line
    assert "1,500 tokens" in line
    assert "1 agent ·" in line  # singular, not "1 agents"
    assert "1 agents" not in line
    assert "Sonnet 5" in line
    assert "[full breakdown](https://example.test/run/1)" in line


def test_usage_line_multiple_models() -> None:
    from ai_pr_review.review.reporting import build_token_usage_line, compute_token_totals

    ar1 = _make_agent_result("code-reviewer", model="claude-sonnet-5")
    ar2 = _make_agent_result("security-reviewer", model="claude-opus-5")
    totals = compute_token_totals([ar1, ar2], _REPO_ROOT)
    line = build_token_usage_line(totals)
    assert "Sonnet 5, Opus 5" in line
    assert "2 agents" in line


def test_usage_line_any_unknown_suffix() -> None:
    from ai_pr_review.review.reporting import build_token_usage_line, compute_token_totals

    totals = compute_token_totals(
        [_make_agent_result(model="some-totally-unpriced-model")], _REPO_ROOT
    )
    assert totals is not None
    assert totals.any_unknown is True
    line = build_token_usage_line(totals)
    assert "$0.0000+" in line


def test_usage_line_no_run_url_omits_link() -> None:
    from ai_pr_review.review.reporting import build_token_usage_line, compute_token_totals

    totals = compute_token_totals([_make_agent_result()], _REPO_ROOT)
    line = build_token_usage_line(totals, run_url="")
    assert "full breakdown" not in line


# ---------------------------------------------------------------------------
# build_high_usage_warning()
# ---------------------------------------------------------------------------


def test_high_usage_warning_disabled_at_zero() -> None:
    from ai_pr_review.review.reporting import build_high_usage_warning, compute_token_totals

    totals = compute_token_totals([_make_agent_result()], _REPO_ROOT)
    assert build_high_usage_warning(totals, 0) == ""


def test_high_usage_warning_silent_at_or_below_threshold() -> None:
    from ai_pr_review.review.reporting import build_high_usage_warning, compute_token_totals

    totals = compute_token_totals([_make_agent_result()], _REPO_ROOT)
    assert totals is not None
    # Cost for 1000 in + 500 out at Sonnet 5 rates is well under $1.
    assert build_high_usage_warning(totals, 1.00) == ""


def test_high_usage_warning_fires_above_threshold() -> None:
    from ai_pr_review.review.reporting import build_high_usage_warning, compute_token_totals

    totals = compute_token_totals([_make_agent_result()], _REPO_ROOT)
    assert totals is not None
    warning = build_high_usage_warning(totals, 0.0001)
    assert warning != ""
    assert "High token usage" in warning
    assert "$0.0001" in warning


def test_high_usage_warning_floor_wording_when_any_unknown() -> None:
    from ai_pr_review.review.reporting import build_high_usage_warning, compute_token_totals

    # An unpriced model contributes 0 to cost_units, so force the threshold
    # low enough that a *priced* companion agent's cost still crosses it,
    # proving the any_unknown floor wording appears rather than the warning
    # being silently suppressed just because part of the run is unpriced.
    ar_priced = _make_agent_result("code-reviewer", model="claude-sonnet-5")
    ar_unpriced = _make_agent_result("mystery-agent", model="some-totally-unpriced-model")
    totals = compute_token_totals([ar_priced, ar_unpriced], _REPO_ROOT)
    assert totals is not None
    assert totals.any_unknown is True
    warning = build_high_usage_warning(totals, 0.0001)
    assert "at least" in warning
    assert "true cost may be higher" in warning


def test_high_usage_warning_never_appears_inside_accordion_string() -> None:
    """#758 decision: the warning is never concatenated into the accordion
    payload — build_token_table_accordion's output must be identical whether
    or not a warning would separately fire for the same run.
    """
    from ai_pr_review.review.reporting import (
        build_high_usage_warning,
        build_token_table_accordion,
        compute_token_totals,
    )

    ar = _make_agent_result()
    accordion = build_token_table_accordion([ar], None, _REPO_ROOT)
    totals = compute_token_totals([ar], _REPO_ROOT)
    warning = build_high_usage_warning(totals, 0.0001)
    assert warning != ""
    assert "High token usage" not in accordion
    assert "⚠️" not in accordion


# ---------------------------------------------------------------------------
# build_full_token_table() — bare table for the job-log echo
# ---------------------------------------------------------------------------


def test_full_token_table_has_no_details_wrapper() -> None:
    from ai_pr_review.review.reporting import build_full_token_table

    table = build_full_token_table([_make_agent_result()], None, _REPO_ROOT)
    assert "<details>" not in table
    assert "<summary>" not in table
    assert "| Agent | Model |" in table


def test_full_token_table_empty_on_no_data() -> None:
    from ai_pr_review.review.reporting import build_full_token_table

    assert build_full_token_table([], None, _REPO_ROOT) == ""


# ---------------------------------------------------------------------------
# ci_run_url()
# ---------------------------------------------------------------------------


def test_ci_run_url_github(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.review.reporting import ci_run_url

    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "tag1consulting/ai-pr-review")
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.delenv("CI_JOB_URL", raising=False)
    monkeypatch.delenv("BITBUCKET_REPO_FULL_NAME", raising=False)
    assert ci_run_url() == "https://github.com/tag1consulting/ai-pr-review/actions/runs/123456"


def test_ci_run_url_gitlab(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.review.reporting import ci_run_url

    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setenv("CI_JOB_URL", "https://gitlab.com/foo/bar/-/jobs/42")
    assert ci_run_url() == "https://gitlab.com/foo/bar/-/jobs/42"


def test_ci_run_url_bitbucket(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.review.reporting import ci_run_url

    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("CI_JOB_URL", raising=False)
    monkeypatch.setenv("BITBUCKET_REPO_FULL_NAME", "myworkspace/myrepo")
    monkeypatch.setenv("BITBUCKET_BUILD_NUMBER", "77")
    assert ci_run_url() == "https://bitbucket.org/myworkspace/myrepo/pipelines/results/77"


def test_ci_run_url_empty_when_no_variables_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.review.reporting import ci_run_url

    for var in (
        "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
        "CI_JOB_URL", "BITBUCKET_REPO_FULL_NAME", "BITBUCKET_BUILD_NUMBER",
    ):
        monkeypatch.delenv(var, raising=False)
    assert ci_run_url() == ""


def test_ci_run_url_github_partial_vars_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed/partial GitHub env (missing RUN_ID) must not produce a
    broken URL — it should fall through to the next platform or "" rather
    than fabricate a link.
    """
    from ai_pr_review.review.reporting import ci_run_url

    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "tag1consulting/ai-pr-review")
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("CI_JOB_URL", raising=False)
    monkeypatch.delenv("BITBUCKET_REPO_FULL_NAME", raising=False)
    assert ci_run_url() == ""


# ---------------------------------------------------------------------------
# _prepare() fail-soft on an unexpected exception during token-log assembly
# (not just a pricing-load failure) -- regression guard. build_full_token_table
# in particular is invoked directly in cli.py's job-log echo, outside of
# orchestrate.run_review()'s own Phase 3.5 try/except, so a bug here must not
# be able to turn an already-successful review into a reported CI failure.
# ---------------------------------------------------------------------------


def test_build_full_token_table_survives_token_log_assembly_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_pr_review.review import reporting

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated bug in token log assembly")

    monkeypatch.setattr(reporting, "_build_token_log", _boom)
    result = reporting.build_full_token_table([_make_agent_result()], None, _REPO_ROOT)
    assert result == ""


def test_build_token_table_accordion_survives_token_log_assembly_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_pr_review.review import reporting

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated bug in token log assembly")

    monkeypatch.setattr(reporting, "_build_token_log", _boom)
    result = reporting.build_token_table_accordion([_make_agent_result()], None, _REPO_ROOT)
    assert result == ""


def test_compute_token_totals_survives_token_log_assembly_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_pr_review.review import reporting

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated bug in token log assembly")

    monkeypatch.setattr(reporting, "_build_token_log", _boom)
    result = reporting.compute_token_totals([_make_agent_result()], _REPO_ROOT)
    assert result is None


def test_prepare_survives_exception_in_context_tokens_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context_tokens/profile_tokens max() computations sit inside the
    same try/except as _build_token_log -- a malformed AgentResult (e.g. a
    non-numeric context_tokens_used from a future refactor) must not escape
    either.
    """
    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage
    from ai_pr_review.review import reporting

    bad_agent_result = AgentResult(
        name="code-reviewer",
        output="",
        token_log=TokenUsage(model="claude-sonnet-5", input=100, output=50,
                             cache_creation=0, cache_read=0),
        truncated=False,
        context_tokens_used="not-a-number",  # type: ignore[arg-type]
    )
    result = reporting.build_full_token_table([bad_agent_result], None, _REPO_ROOT)
    assert result == ""
