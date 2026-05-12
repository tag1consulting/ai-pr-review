"""Tests for ai_pr_review.cli."""

import json
import os
from unittest.mock import patch

from click.testing import CliRunner

from ai_pr_review.cli import _run_compute, cli
from ai_pr_review.config import ReviewConfig
from ai_pr_review.diff.compute import DiffResult

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
) -> DiffResult:
    return DiffResult(
        diff_text=diff_text,
        changed_files=changed_files if changed_files is not None else ["src/main.py"],
        diff_stat="1 file changed, 1 insertion(+)",
        diff_label="full (main..abc1234)",
        base="main",
        head="abc1234",
        is_incremental=False,
    )


# ---------------------------------------------------------------------------
# _run_compute
# ---------------------------------------------------------------------------


class TestRunCompute:
    def test_no_changed_files_returns_skip(self) -> None:
        config = _make_config()
        diff = _make_diff_result(changed_files=[])
        with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
            result = _run_compute(config)
        assert result["skip"] is True
        assert result["reason"] == "no changed files"

    def test_diff_too_large_returns_skip(self) -> None:
        config = _make_config(max_diff_lines=5)
        # 10 lines of diff
        diff_text = "\n".join([f"+line {i}" for i in range(10)])
        diff = _make_diff_result(diff_text=diff_text)
        with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
            result = _run_compute(config)
        assert result["skip"] is True
        assert "too large" in str(result["reason"])

    def test_diff_at_limit_not_skipped(self) -> None:
        config = _make_config(max_diff_lines=5)
        diff_text = "\n".join([f"+line {i}" for i in range(5)])  # exactly 5 lines
        diff = _make_diff_result(diff_text=diff_text)
        with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
            result = _run_compute(config)
        assert result["skip"] is False

    def test_max_diff_lines_zero_disables_limit(self) -> None:
        config = _make_config(max_diff_lines=0)
        diff_text = "\n".join([f"+line {i}" for i in range(100)])
        diff = _make_diff_result(diff_text=diff_text)
        with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
            result = _run_compute(config)
        assert result["skip"] is False

    def test_successful_run_returns_payload(self) -> None:
        config = _make_config()
        diff = _make_diff_result()
        with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
            result = _run_compute(config)
        assert result["skip"] is False
        assert result["diff"] == diff.diff_text
        assert result["changed_files"] == ["src/main.py"]
        assert "manifest" in result
        assert "languages" in result
        assert result["is_incremental"] is False

    def test_payload_includes_empty_findings_and_token_log(self) -> None:
        config = _make_config()
        diff = _make_diff_result()
        with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
            result = _run_compute(config)
        assert result["findings"] == []
        assert result["token_log"] == []


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


class TestComputeCommand:
    def _base_env(self) -> dict[str, str]:
        return {
            "BASE_REF": "main",
            "HEAD_SHA": "abc1234",
            "PR_NUMBER": "42",
            "AI_PR_REVIEW_ENGINE": "python",
        }

    def test_config_error_exits_1(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli,
                ["compute"],
                env={"AI_UNKNOWN_BLAH": "1", "BASE_REF": "main", "HEAD_SHA": "x"},
                catch_exceptions=False,
            )
        assert result.exit_code == 1
        assert "Configuration error" in result.output

    def test_writes_to_output_file(self) -> None:
        runner = CliRunner()
        diff = _make_diff_result()
        with runner.isolated_filesystem():
            output_path = "compute-output.json"
            with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
                result = runner.invoke(
                    cli,
                    ["compute", "--output", output_path],
                    env=self._base_env(),
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, result.output
            with open(output_path) as fh:
                payload = json.loads(fh.read())
            assert "skip" in payload

    def test_prints_to_stdout_when_no_output(self) -> None:
        runner = CliRunner()
        diff = _make_diff_result()
        with runner.isolated_filesystem():
            with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
                result = runner.invoke(
                    cli,
                    ["compute"],
                    env=self._base_env(),
                    catch_exceptions=False,
                )
            assert result.exit_code == 0
            # JSON should be on stdout
            payload = json.loads(result.output)
            assert "skip" in payload

    def test_unwritable_output_exits_1(self) -> None:
        runner = CliRunner()
        diff = _make_diff_result()
        with runner.isolated_filesystem():
            with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
                result = runner.invoke(
                    cli,
                    ["compute", "--output", "/nonexistent/dir/out.json"],
                    env=self._base_env(),
                    catch_exceptions=False,
                )
            assert result.exit_code == 1
            assert "ERROR" in result.output

    def test_output_env_var_used(self) -> None:
        runner = CliRunner()
        diff = _make_diff_result()
        with runner.isolated_filesystem():
            env = {**self._base_env(), "AI_PR_REVIEW_COMPUTE_OUTPUT": "from-env.json"}
            with patch("ai_pr_review.diff.compute.compute_diff", return_value=diff):
                result = runner.invoke(
                    cli,
                    ["compute"],
                    env=env,
                    catch_exceptions=False,
                )
            assert result.exit_code == 0
            assert os.path.exists("from-env.json")
