"""Tests for the shared preflight-agent gating/cap predicates (#848 item 2).

should_run_summarizer/should_run_issue_linker/preflight_agent_max_tokens are
the single source of truth cli.py's real dispatch and review/runtime.py's
pre-flight cost estimate both call, so this file is the anti-drift guard for
that agreement.
"""

from __future__ import annotations

import pytest

from ai_pr_review.review.preflight import (
    preflight_agent_max_tokens,
    should_run_issue_linker,
    should_run_summarizer,
)


class TestShouldRunSummarizer:
    def test_runs_on_first_review_with_no_exclusions(self) -> None:
        assert should_run_summarizer(is_incremental=False, agents=(), exclude_agents=()) is True

    def test_skipped_on_incremental_run(self) -> None:
        assert should_run_summarizer(is_incremental=True, agents=(), exclude_agents=()) is False

    def test_skipped_when_excluded(self) -> None:
        assert should_run_summarizer(
            is_incremental=False, agents=(), exclude_agents=("pr-summarizer",),
        ) is False

    def test_skipped_when_allowlist_excludes_it(self) -> None:
        assert should_run_summarizer(
            is_incremental=False, agents=("code-reviewer",), exclude_agents=(),
        ) is False

    def test_runs_when_allowlist_includes_it(self) -> None:
        assert should_run_summarizer(
            is_incremental=False, agents=("pr-summarizer", "code-reviewer"), exclude_agents=(),
        ) is True


class TestShouldRunIssueLinker:
    def test_runs_on_first_full_mode_github_review(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False, review_mode="full", vcs_provider="github",
            agents=(), exclude_agents=(),
        ) is True

    def test_skipped_on_incremental_run(self) -> None:
        assert should_run_issue_linker(
            is_incremental=True, review_mode="full", vcs_provider="github",
            agents=(), exclude_agents=(),
        ) is False

    def test_skipped_in_quick_mode(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False, review_mode="quick", vcs_provider="github",
            agents=(), exclude_agents=(),
        ) is False

    def test_skipped_on_non_github_provider(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False, review_mode="full", vcs_provider="gitlab",
            agents=(), exclude_agents=(),
        ) is False

    def test_skipped_when_excluded(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False, review_mode="full", vcs_provider="github",
            agents=(), exclude_agents=("issue-linker",),
        ) is False


class TestPreflightAgentMaxTokens:
    def test_defaults_to_roster_value_with_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AI_MAX_TOKENS_PR_SUMMARIZER", raising=False)
        from ai_pr_review.agents.roster import get_agent

        assert preflight_agent_max_tokens("pr-summarizer") == get_agent("pr-summarizer").max_output_tokens

    def test_per_agent_override_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_MAX_TOKENS_PR_SUMMARIZER", "8192")
        assert preflight_agent_max_tokens("pr-summarizer") == 8192

    def test_per_agent_override_applies_to_issue_linker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_MAX_TOKENS_ISSUE_LINKER", "500")
        assert preflight_agent_max_tokens("issue-linker") == 500

    # The real anti-drift proof -- that this resolution equals what
    # run_summarizer/run_issue_linker actually put on their LLMRequest --
    # is tests/python/test_pr_summarizer_preflight.py's
    # test_default_max_tokens_resolves_from_roster and
    # test_per_agent_max_tokens_override_applied (and the issue-linker
    # equivalents in tests/python/test_issue_linker.py), which drive the
    # real functions end-to-end rather than re-deriving the value here.
