"""Unit tests for _build_token_table_accordion.

Tests that the pure renderer returns a non-empty accordion on valid agent
data, returns "" on empty input, and is fail-soft on bad pricing config.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers — minimal fake agent results
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[2]


def _make_agent_result(name: str = "code-reviewer") -> object:
    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage

    tl = TokenUsage(model="claude-haiku-4-5", input=100, output=50,
                    cache_creation=0, cache_read=0)
    return AgentResult(name=name, output="", token_log=tl,
                       truncated=False, context_tokens_used=0)


# ---------------------------------------------------------------------------
# _build_token_table_accordion
# ---------------------------------------------------------------------------

def test_returns_accordion_on_valid_data() -> None:
    from ai_pr_review.cli import _build_token_table_accordion

    ar = _make_agent_result()
    result = _build_token_table_accordion([ar], None, _REPO_ROOT)
    assert result.startswith("<details>")
    assert "<summary>Token usage by agent</summary>" in result
    assert result.rstrip().endswith("</details>")


def test_returns_empty_string_when_no_agent_results() -> None:
    from ai_pr_review.cli import _build_token_table_accordion

    result = _build_token_table_accordion([], None, _REPO_ROOT)
    assert result == ""


def test_returns_empty_string_when_token_log_is_none() -> None:
    from ai_pr_review.agents.dispatch import AgentResult
    from ai_pr_review.cli import _build_token_table_accordion

    ar = AgentResult(name="code-reviewer", output="", token_log=None,
                     truncated=False, context_tokens_used=0)
    result = _build_token_table_accordion([ar], None, _REPO_ROOT)
    assert result == ""


def test_fail_soft_on_missing_pricing_file(tmp_path: Path) -> None:
    from ai_pr_review.cli import _build_token_table_accordion

    ar = _make_agent_result()
    # Pass a script_dir whose config/model-pricing.json doesn't exist.
    # load_pricing() warns but continues with n/a costs — the accordion is
    # still returned (not ""), which is the correct fail-soft behaviour.
    result = _build_token_table_accordion([ar], None, tmp_path)
    assert "<details>" in result


def test_includes_agent_name_in_output() -> None:
    from ai_pr_review.cli import _build_token_table_accordion

    ar = _make_agent_result("security-reviewer")
    result = _build_token_table_accordion([ar], None, _REPO_ROOT)
    assert "security-reviewer" in result


def test_sarif_elapsed_forwarded() -> None:
    from ai_pr_review.cli import _build_token_table_accordion

    ar = _make_agent_result()
    result = _build_token_table_accordion([ar], 0.42, _REPO_ROOT)
    # The SARIF row appears when sarif_elapsed_s is non-None; verify the
    # accordion is still well-formed.
    assert "<details>" in result
    assert "</details>" in result


def test_context_tokens_forwarded() -> None:
    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage
    from ai_pr_review.cli import _build_token_table_accordion

    tl = TokenUsage(model="claude-haiku-4-5", input=200, output=80,
                    cache_creation=0, cache_read=0)
    ar = AgentResult(name="code-reviewer", output="", token_log=tl,
                     truncated=False, context_tokens_used=500)
    result = _build_token_table_accordion([ar], None, _REPO_ROOT)
    assert "<details>" in result
