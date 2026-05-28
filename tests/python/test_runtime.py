"""Tests for the review runtime assembly boundary (ai_pr_review.review.runtime)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ai_pr_review.config import ReviewConfig
from ai_pr_review.diff.compute import DiffResult
from ai_pr_review.findings.models import Finding
from ai_pr_review.review.runtime import ReviewRuntime, SkipPlan, build_review_runtime
from ai_pr_review.vcs.protocol import (
    FindingsResult,
    StaleResult,
    SummaryResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs: object) -> ReviewConfig:
    defaults: dict[str, object] = {
        "base_ref": "main",
        "head_sha": "abc1234",
        "max_diff_lines": 5000,
        "review_mode": "quick",
        "vcs_provider": "github",
        "provider": "anthropic",
        "pr_number": "42",
        "confidence_threshold": 75,
        "review_target": "pr",
        "engine": "python",
        "compute_output": "",
        "parallel": True,
        "max_inline": 25,
        "max_tokens_per_agent": 8192,
        "enable_suggestions": True,
        "temperature": 0.3,
        "llm_prompt_caching": "auto",
        "cache_priming": False,
        "llm_retry_count": 3,
        "force_full_diff": False,
        "standalone_depth": 50,
        "phpstan_level": 3,
        "disable_gate_architecture": False,
        "disable_gate_security": False,
        "disable_gate_edge_case": False,
        "model_standard": "claude-sonnet-4-6",
        "model_premium": "claude-opus-4-7",
        "bitbucket_email": "",
        "bitbucket_api_token": "",
        "bitbucket_workspace": "",
        "bitbucket_repo_slug": "",
        "gitlab_token": "",
        "gitlab_api_url": "https://gitlab.com",
        "gitlab_project_id": "",
        "gitlab_mr_diff_base_sha": "",
        "gitlab_bot_username": "",
    }
    defaults.update(kwargs)
    return ReviewConfig.model_validate(defaults)


def _make_diff_result(
    changed_files: list[str] | None = None,
    diff_text: str = "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,2 @@\n+new line\n",
    is_incremental: bool = False,
) -> DiffResult:
    return DiffResult(
        diff_text=diff_text,
        changed_files=changed_files if changed_files is not None else ["src/main.py"],
        diff_stat="1 file changed, 1 insertion(+)",
        diff_label="full (main..abc1234)",
        base="main",
        head="abc1234",
        is_incremental=is_incremental,
    )


@dataclass
class _FakeProvider:
    """Minimal VcsProvider fake that satisfies the @runtime_checkable protocol."""

    last_sha: str | None = None
    post_summary_calls: list[Any] = field(default_factory=list)
    post_findings_calls: list[Any] = field(default_factory=list)

    def get_last_reviewed_sha(self) -> str | None:
        return self.last_sha

    def get_summary_body(self) -> str | None:
        return None

    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        self.post_summary_calls.append((summary_body, head_sha))
        return SummaryResult(comment_id=1, created=True, updated=False)

    def post_findings(
        self,
        findings: Sequence[Finding],
        diff: Any,
        *,
        event: str,
        failed_agents: Sequence[str] = (),
        token_table: str = "",
        agent_prompt: str = "",
        max_inline: int = 25,
        enable_suggestions: bool = True,
    ) -> FindingsResult:
        self.post_findings_calls.append(findings)
        return FindingsResult(review_id=1, inline_posted=0, body_findings=0, event="COMMENT")

    def resolve_stale(self) -> StaleResult:
        return StaleResult(threads_resolved=0, reviews_dismissed=0)

    def advance_sha_watermark(self, new_sha: str) -> bool:
        return False

    def post_skip_comment(self, reason: str) -> SummaryResult:
        return SummaryResult(comment_id=None, created=False, updated=False)


def _make_fake_provider(last_sha: str | None = None) -> _FakeProvider:
    provider = _FakeProvider(last_sha=last_sha)
    return provider


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildReviewRuntimeFullPath:
    """build_review_runtime() returns a populated ReviewRuntime on the happy path."""

    def test_returns_review_runtime(self, tmp_path: Path) -> None:
        config = _make_config()
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            result = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(result, ReviewRuntime)

    def test_runtime_has_non_empty_agents(self, tmp_path: Path) -> None:
        config = _make_config(review_mode="full")
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert len(runtime.agents) > 0, "expected at least one agent in full mode"

    def test_runtime_orch_config_has_no_sarif_paths(self, tmp_path: Path) -> None:
        """OrchestrationConfig no longer has sarif_paths — verify field is absent."""
        config = _make_config()
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert not hasattr(runtime.orch_config, "sarif_paths")

    def test_runtime_carries_resolved_config(self, tmp_path: Path) -> None:
        """runtime.config is the post-resolve_models() copy — models should be set."""
        config = _make_config(model_standard="claude-sonnet-4-6", model_premium="claude-opus-4-7")
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert runtime.config.model_standard == "claude-sonnet-4-6"
        assert runtime.config.model_premium == "claude-opus-4-7"

    def test_provider_factory_called_exactly_once(self, tmp_path: Path) -> None:
        config = _make_config()
        provider = _make_fake_provider()
        call_count = 0

        def _factory() -> _FakeProvider:
            nonlocal call_count
            call_count += 1
            return provider

        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            build_review_runtime(config, provider_factory=_factory)

        assert call_count == 1


class TestBuildReviewRuntimeSkip:
    """build_review_runtime() returns SkipPlan when compute says skip."""

    def test_returns_skip_plan_on_no_changed_files(self) -> None:
        config = _make_config()
        provider = _make_fake_provider()

        with patch(
            "ai_pr_review.diff.compute.compute_diff",
            return_value=_make_diff_result(changed_files=[]),
        ):
            result = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(result, SkipPlan)
        assert result.reason == "no changed files"

    def test_skip_plan_carries_provider(self) -> None:
        config = _make_config()
        provider = _make_fake_provider()

        with patch(
            "ai_pr_review.diff.compute.compute_diff",
            return_value=_make_diff_result(changed_files=[]),
        ):
            result = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(result, SkipPlan)
        assert result.provider is provider

    def test_returns_skip_plan_on_diff_too_large(self) -> None:
        config = _make_config(max_diff_lines=3)
        provider = _make_fake_provider()
        big_diff = "\n".join([f"+line {i}" for i in range(10)])

        with patch(
            "ai_pr_review.diff.compute.compute_diff",
            return_value=_make_diff_result(diff_text=big_diff),
        ):
            result = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(result, SkipPlan)
        assert "too large" in result.reason


class TestSarifRoutedViaExtraFindings:
    """SARIF findings from config.sarif_paths flow through orch_config.extra_findings."""

    def test_sarif_paths_loaded_into_extra_findings(self, tmp_path: Path) -> None:
        sarif_finding = Finding(
            finding="test-rule-id",
            path="src/main.py",
            line=5,
            severity="high",
            title="Test SARIF finding",
            body="details",
            source="semgrep",
            confidence=90,
        )
        config = _make_config(sarif_paths=(str(tmp_path / "results.sarif"),))
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch(
                "ai_pr_review.analyzers.sarif.load_sarif_files",
                return_value=([sarif_finding], 0.1),
            ),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert sarif_finding in runtime.orch_config.extra_findings

    def test_sarif_elapsed_s_propagated_to_runtime(self, tmp_path: Path) -> None:
        """sarif_elapsed_s from load_sarif_files is stored on ReviewRuntime, not discarded."""
        config = _make_config(sarif_paths=(str(tmp_path / "results.sarif"),))
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch(
                "ai_pr_review.analyzers.sarif.load_sarif_files",
                return_value=([], 1.23),
            ),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert runtime.sarif_elapsed_s == 1.23

    def test_sarif_elapsed_s_is_none_when_no_sarif_paths(self, tmp_path: Path) -> None:
        """sarif_elapsed_s is None when no SARIF paths are configured."""
        config = _make_config()
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert runtime.sarif_elapsed_s is None


class TestExplicitConfigNoBashDefaults:
    """Explicit model fields are not overridden by provider defaults (#319 regression guard)."""

    def test_explicit_models_preserved(self, tmp_path: Path) -> None:
        config = _make_config(
            provider="openai",
            model_standard="gpt-custom-standard",
            model_premium="gpt-custom-premium",
        )
        provider = _make_fake_provider()
        diff_file = tmp_path / "diff.txt"

        with (
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(diff_file)}, clear=False),
        ):
            runtime = build_review_runtime(config, provider_factory=lambda: provider)

        assert isinstance(runtime, ReviewRuntime)
        assert runtime.config.model_standard == "gpt-custom-standard"
        assert runtime.config.model_premium == "gpt-custom-premium"
        assert runtime.dispatch_context.standard_model == "gpt-custom-standard"
        assert runtime.dispatch_context.premium_model == "gpt-custom-premium"
