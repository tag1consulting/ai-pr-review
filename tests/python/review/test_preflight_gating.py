"""Tests for the shared preflight-agent dispatch gates (#848).

should_run_pr_summarizer/should_run_issue_linker are the single source of
truth for "will this separately-dispatched preflight agent actually run
this review", shared between cli.py's real dispatch gate and
review/runtime.py's pre-flight cost estimate -- previously each reimplemented
the same condition inline, with no test forcing them to agree.
"""

from __future__ import annotations

from ai_pr_review.review.preflight import (
    PREFLIGHT_AGENT_MAX_TOKENS,
    should_run_issue_linker,
    should_run_pr_summarizer,
)


class TestShouldRunPrSummarizer:
    def test_true_on_first_review_when_allowed(self) -> None:
        assert should_run_pr_summarizer(
            is_incremental=False, agents=(), exclude_agents=(),
        ) is True

    def test_false_on_incremental_run(self) -> None:
        assert should_run_pr_summarizer(
            is_incremental=True, agents=(), exclude_agents=(),
        ) is False

    def test_false_when_excluded(self) -> None:
        assert should_run_pr_summarizer(
            is_incremental=False, agents=(), exclude_agents=("pr-summarizer",),
        ) is False

    def test_false_when_not_in_explicit_allowlist(self) -> None:
        assert should_run_pr_summarizer(
            is_incremental=False, agents=("code-reviewer",), exclude_agents=(),
        ) is False

    def test_true_when_explicitly_allowlisted(self) -> None:
        assert should_run_pr_summarizer(
            is_incremental=False, agents=("pr-summarizer",), exclude_agents=(),
        ) is True


class TestShouldRunIssueLinker:
    def test_true_on_first_full_mode_github_review(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False,
            review_mode="full",
            vcs_provider="github",
            agents=(),
            exclude_agents=(),
        ) is True

    def test_false_on_incremental_run(self) -> None:
        assert should_run_issue_linker(
            is_incremental=True,
            review_mode="full",
            vcs_provider="github",
            agents=(),
            exclude_agents=(),
        ) is False

    def test_false_in_quick_mode(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False,
            review_mode="quick",
            vcs_provider="github",
            agents=(),
            exclude_agents=(),
        ) is False

    def test_false_on_non_github_provider(self) -> None:
        for provider in ("gitlab", "bitbucket"):
            assert should_run_issue_linker(
                is_incremental=False,
                review_mode="full",
                vcs_provider=provider,
                agents=(),
                exclude_agents=(),
            ) is False

    def test_false_when_excluded(self) -> None:
        assert should_run_issue_linker(
            is_incremental=False,
            review_mode="full",
            vcs_provider="github",
            agents=(),
            exclude_agents=("issue-linker",),
        ) is False


def test_preflight_agent_max_tokens_matches_documented_value() -> None:
    """Pinned so a future change to either LLMRequest.max_tokens call site
    in run_summarizer/run_issue_linker is forced through this one constant
    rather than silently drifting back into two independent literals."""
    assert PREFLIGHT_AGENT_MAX_TOKENS == 4096
