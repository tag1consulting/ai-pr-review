"""Tests for ai_pr_review.cli."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from ai_pr_review.cli import cli
from ai_pr_review.config import ReviewConfig
from ai_pr_review.diff.compute import DiffResult
from ai_pr_review.review.compute import run_compute as _run_compute

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


class TestScriptDir:
    """script_dir resolution in _run_review_async."""

    def _make_provider_mock(self) -> MagicMock:
        """Build a MagicMock that satisfies the VcsProvider runtime_checkable protocol."""
        from ai_pr_review.vcs.protocol import VcsProvider

        provider = MagicMock(spec=VcsProvider)
        provider.get_last_reviewed_sha.return_value = None
        return provider

    def _fake_run_review_factory(self, captured: list) -> AsyncMock:  # type: ignore[type-arg]
        async def _fake(**kwargs: object) -> object:
            captured.append(kwargs.get("dispatch_context"))
            result = MagicMock()
            result.ok = True
            result.skipped = False
            result.findings = []
            result.failed_agents = []
            result.outcome = MagicMock()
            result.outcome.event = "COMMENT"
            return result

        return AsyncMock(side_effect=_fake)

    def test_env_var_overrides_package_path(self, tmp_path: Path) -> None:
        """AI_PR_REVIEW_SCRIPT_DIR must take priority over __file__-relative path."""
        import anyio

        from ai_pr_review.agents.dispatch import DispatchContext

        sentinel = str(tmp_path / "fake_script_dir")
        os.makedirs(sentinel, exist_ok=True)
        diff_file = str(tmp_path / "diff.txt")

        captured: list[DispatchContext] = []

        with (
            patch.dict(
                os.environ,
                {"AI_PR_REVIEW_SCRIPT_DIR": sentinel, "AI_PR_REVIEW_DIFF_FILE": diff_file},
            ),
            patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
            patch("ai_pr_review.review.runtime.provider_from_env", return_value=self._make_provider_mock()),
            patch("ai_pr_review.orchestrate.run_review", new=self._fake_run_review_factory(captured)),
            patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
            patch("ai_pr_review.agents.roster.AGENTS", []),
            patch("ai_pr_review.cli._run_summarizer", return_value=""),
        ):
            from ai_pr_review.cli import _run_review_async

            anyio.run(_run_review_async, _make_config())

        assert captured, "run_review was not called"
        assert str(captured[0].script_dir) == sentinel

    def test_falls_back_to_package_path_when_env_unset(self, tmp_path: Path) -> None:
        """Without AI_PR_REVIEW_SCRIPT_DIR, script_dir is derived from __file__."""
        import anyio

        from ai_pr_review.agents.dispatch import DispatchContext

        captured: list[DispatchContext] = []
        # runtime.py is at ai_pr_review/review/runtime.py; parent.parent.parent = repo root
        from ai_pr_review.review import runtime as runtime_mod
        expected = Path(runtime_mod.__file__).resolve().parent.parent.parent  # type: ignore[arg-type]
        diff_file = str(tmp_path / "diff.txt")

        # Remove only AI_PR_REVIEW_SCRIPT_DIR so the fallback path is taken,
        # without stripping unrelated vars (PATH, HOME, TMPDIR) that anyio needs.
        saved = os.environ.pop("AI_PR_REVIEW_SCRIPT_DIR", None)
        try:
            with (
                patch.dict(os.environ, {"AI_PR_REVIEW_DIFF_FILE": diff_file}),
                patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()),
                patch("ai_pr_review.review.runtime.provider_from_env", return_value=self._make_provider_mock()),
                patch("ai_pr_review.orchestrate.run_review", new=self._fake_run_review_factory(captured)),
                patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
                patch("ai_pr_review.agents.roster.AGENTS", []),
                patch("ai_pr_review.cli._run_summarizer", return_value=""),
            ):
                from ai_pr_review.cli import _run_review_async

                anyio.run(_run_review_async, _make_config())
        finally:
            if saved is not None:
                os.environ["AI_PR_REVIEW_SCRIPT_DIR"] = saved

        assert captured, "run_review was not called"
        assert captured[0].script_dir == expected


class TestParseChangedFilesPayload:
    """parse_changed_files_payload normalisation and warning."""

    def test_malformed_entry_emits_warning(self) -> None:
        """Empty-path entries must emit logger.warning, not crash or silently drop."""
        import logging
        from unittest.mock import patch

        from ai_pr_review.manifest import parse_changed_files_payload

        with patch.object(logging.getLogger("ai_pr_review.manifest"), "warning") as mock_warn:
            parse_changed_files_payload([{"path": ""}, {"path": "src/main.py"}, {}])
        # Two malformed entries: dict with empty path, and bare empty dict
        assert mock_warn.call_count == 2

    def test_valid_entries_are_included(self) -> None:
        """Valid path strings and dicts are included in the result."""
        from ai_pr_review.manifest import parse_changed_files_payload

        result = parse_changed_files_payload(["a.py", {"path": "b.py"}])
        assert result is not None
        assert "a.py" in result.all_files
        assert "b.py" in result.all_files


class TestWriteStepSummary:
    """Tests for _write_step_summary (E4.S3 — GITHUB_STEP_SUMMARY output)."""

    def _make_runtime(self, tmp_path: Path) -> object:
        """Build a minimal ReviewRuntime-like object for step summary tests."""
        from pathlib import Path as _Path
        from unittest.mock import MagicMock

        from ai_pr_review.manifest import parse_changed_files_payload

        rt = MagicMock()
        rt.changed_files = parse_changed_files_payload(["foo.py", "bar.py"])
        rt.config.review_mode = "full"
        rt.agents = [MagicMock(), MagicMock()]
        rt.sarif_elapsed_s = None
        rt.script_dir = _Path(".")
        rt.dispatch_context.max_tokens_per_agent = 8192
        return rt

    def _make_result(self) -> object:
        from unittest.mock import MagicMock

        from ai_pr_review.findings.models import Finding
        result = MagicMock()
        result.findings = [
            Finding(severity="High", file="foo.py", line=1,
                    finding="x", remediation="y", agent="code-reviewer",
                    source="code-reviewer", confidence=90),
        ]
        result.failed_agents = []
        result.agent_results = []
        result.skipped = False
        return result

    def test_writes_to_step_summary_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When GITHUB_STEP_SUMMARY is set, a summary file is written."""
        from ai_pr_review.cli import _write_step_summary

        summary_path = tmp_path / "step_summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

        _write_step_summary(self._make_result(), self._make_runtime(tmp_path), "PR summary text")

        content = summary_path.read_text()
        assert "## AI PR Review Results" in content
        assert "**Mode:** full" in content
        assert "**Findings:** 1" in content
        assert "PR summary text" in content

    def test_no_op_when_env_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When GITHUB_STEP_SUMMARY is not set, no file is created."""
        from ai_pr_review.cli import _write_step_summary

        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        # Should not raise and should not create any file
        _write_step_summary(self._make_result(), self._make_runtime(tmp_path), "summary")
        assert not list(tmp_path.glob("*.md"))

    def test_non_runtime_object_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-ReviewRuntime runtime arg is silently skipped."""
        from ai_pr_review.cli import _write_step_summary

        summary_path = tmp_path / "step_summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
        _write_step_summary(self._make_result(), object(), "summary")
        assert not summary_path.exists()

    def test_failed_agents_listed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Failed agent names appear in the step summary."""
        from unittest.mock import MagicMock

        from ai_pr_review.cli import _write_step_summary

        summary_path = tmp_path / "step_summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

        result = self._make_result()
        fa = MagicMock()
        fa.name = "security-reviewer"
        result.failed_agents = [fa]

        _write_step_summary(result, self._make_runtime(tmp_path), "")
        content = summary_path.read_text()
        assert "security-reviewer" in content

    def test_oserror_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  caplog: pytest.LogCaptureFixture) -> None:
        """An OSError writing the file logs a warning and does not propagate."""
        import logging

        from ai_pr_review.cli import _write_step_summary

        monkeypatch.setenv("GITHUB_STEP_SUMMARY", "/nonexistent/dir/step_summary.md")
        with caplog.at_level(logging.WARNING):
            _write_step_summary(self._make_result(), self._make_runtime(tmp_path), "")
        assert any("step summary" in r.message.lower() for r in caplog.records)
