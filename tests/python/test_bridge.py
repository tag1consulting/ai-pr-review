"""Tests for ai_pr_review.analyzers.bridge."""

import json
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_pr_review.analyzers.bridge import (
    AnalyzerSpec,
    _is_eligible,
    _normalise_output,
    run_analyzers,
)
from ai_pr_review.manifest import ChangedFiles

# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------


class TestIsEligible:
    def test_no_required_types_always_eligible(self) -> None:
        spec = AnalyzerSpec("trufflehog", "run-trufflehog.sh", [])
        cf = ChangedFiles()
        assert _is_eligible(spec, cf) is True

    def test_required_type_present(self) -> None:
        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])
        cf = ChangedFiles(shell=["review.sh"])
        assert _is_eligible(spec, cf) is True

    def test_required_type_absent(self) -> None:
        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])
        cf = ChangedFiles()
        assert _is_eligible(spec, cf) is False

    def test_multi_type_any_match(self) -> None:
        spec = AnalyzerSpec("checkov", "run-checkov.sh", ["terraform", "iac", "dockerfile"])
        cf = ChangedFiles(dockerfile=["Dockerfile"])
        assert _is_eligible(spec, cf) is True

    def test_multi_type_none_match(self) -> None:
        spec = AnalyzerSpec("checkov", "run-checkov.sh", ["terraform", "iac", "dockerfile"])
        cf = ChangedFiles(python=["main.py"])
        assert _is_eligible(spec, cf) is False


# ---------------------------------------------------------------------------
# _normalise_output
# ---------------------------------------------------------------------------


class TestNormaliseOutput:
    def test_valid_findings(self) -> None:
        data = [{"severity": "High", "confidence": 80, "finding": "SQL injection risk"}]
        result = _normalise_output(json.dumps(data), "test-analyzer")
        assert len(result) == 1
        assert result[0].severity == "High"
        assert result[0].source == "test-analyzer"

    def test_source_from_data_takes_precedence(self) -> None:
        data = [{"severity": "Low", "confidence": 60, "finding": "X", "source": "custom"}]
        result = _normalise_output(json.dumps(data), "fallback")
        assert result[0].source == "custom"

    def test_non_json_returns_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _normalise_output("not json", "test-analyzer")
        assert result == []
        captured = capsys.readouterr()
        assert "non-JSON" in captured.err

    def test_non_list_returns_empty(self) -> None:
        result = _normalise_output(json.dumps({"key": "val"}), "test-analyzer")
        assert result == []

    def test_malformed_finding_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Missing required 'finding' field
        data = [{"severity": "High", "confidence": 80}]
        result = _normalise_output(json.dumps(data), "test-analyzer")
        assert result == []
        captured = capsys.readouterr()
        assert "malformed" in captured.err

    def test_non_dict_items_skipped(self) -> None:
        data = ["not a dict", {"severity": "Low", "confidence": 50, "finding": "ok"}]
        result = _normalise_output(json.dumps(data), "test-analyzer")
        assert len(result) == 1

    def test_empty_array(self) -> None:
        result = _normalise_output("[]", "test-analyzer")
        assert result == []


# ---------------------------------------------------------------------------
# run_analyzers
# ---------------------------------------------------------------------------


class TestRunAnalyzers:
    def _make_script(self, tmpdir: str, name: str, output: str, exit_code: int = 0) -> Path:
        """Create a mock bash script in tmpdir/analyzers/ that prints output."""
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir(exist_ok=True)
        script = analyzers / name
        script.write_text(f"#!/bin/bash\necho '{output}'\nexit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_skips_analyzer_with_no_eligible_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # shellcheck requires shell files
            self._make_script(tmpdir, "run-shellcheck.sh", "[]")
            cf = ChangedFiles()  # no shell files
            findings = run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []

    def test_runs_eligible_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = json.dumps([{"severity": "Low", "confidence": 55, "finding": "SC2034"}])
            self._make_script(tmpdir, "run-shellcheck.sh", payload)
            cf = ChangedFiles(shell=["review.sh"])
            findings = run_analyzers(cf, "/dev/null", tmpdir)
            assert len(findings) == 1

    def test_missing_script_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Script file does not exist
            cf = ChangedFiles(shell=["review.sh"])
            findings = run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []

    def test_non_zero_exit_code_other_than_1_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_script(tmpdir, "run-shellcheck.sh", "[]", exit_code=2)
            cf = ChangedFiles(shell=["review.sh"])
            findings = run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []
            captured = capsys.readouterr()
            assert "exited 2" in captured.err

    def test_exit_code_1_accepted(self) -> None:
        """Exit code 1 is valid (grep returns 1 for no-match paths)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_script(tmpdir, "run-shellcheck.sh", "[]", exit_code=1)
            cf = ChangedFiles(shell=["review.sh"])
            findings = run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []  # empty but not an error

    def test_timeout_returns_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        spec = AnalyzerSpec("slow", "run-slow.sh", [])
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 120)),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-slow.sh")
            script.write_text("#!/bin/bash\nsleep 999\n")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            cf = ChangedFiles()
            from ai_pr_review.analyzers import bridge
            with patch.object(bridge, "_ANALYZERS", [spec]):
                findings = run_analyzers(cf, "/dev/null", tmpdir)
        assert findings == []
        captured = capsys.readouterr()
        assert "timed out" in captured.err

    def test_oserror_returns_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-shellcheck.sh")
            script.write_text("#!/bin/bash\n")
            # Remove execute permission to trigger OSError on some systems
            script.chmod(0o644)
            cf = ChangedFiles(shell=["review.sh"])
            with patch("subprocess.run", side_effect=OSError("permission denied")):
                findings = run_analyzers(cf, "/dev/null", tmpdir)
        assert findings == []
        captured = capsys.readouterr()
        assert "failed to start" in captured.err

    def test_empty_stdout_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Script produces no output
            self._make_script(tmpdir, "run-shellcheck.sh", "")
            cf = ChangedFiles(shell=["review.sh"])
            findings = run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []

    def test_env_vars_passed_to_subprocess(self) -> None:
        """DIFF_FILE must be set in the subprocess environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzers = Path(tmpdir) / "analyzers"
            analyzers.mkdir()
            script = analyzers / "run-shellcheck.sh"
            # Use printf to avoid shell quoting issues with echo and double quotes
            script.write_text(
                "#!/bin/bash\n"
                'printf \'[{"severity":"Low","confidence":50,"finding":"%s"}]\\n\' "$DIFF_FILE"\n'
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            cf = ChangedFiles(shell=["review.sh"])
            findings = run_analyzers(cf, "/tmp/test.diff", tmpdir)
            assert len(findings) == 1
            assert "/tmp/test.diff" in findings[0].finding


# ---------------------------------------------------------------------------
# Warning format assertions (Story 4-5)
# ---------------------------------------------------------------------------


class TestWarningFormat:
    def test_timeout_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        spec = AnalyzerSpec("slow-tool", "run-slow.sh", [])
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="bash", timeout=120)),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-slow.sh")
            script.write_text("#!/bin/bash\nsleep 9999\n")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            from ai_pr_review.analyzers import bridge
            with patch.object(bridge, "_ANALYZERS", [spec]):
                findings = run_analyzers(ChangedFiles(), "/dev/null", tmpdir)
        captured = capsys.readouterr()
        assert findings == []
        assert "[ai-pr-review] WARNING:" in captured.err

    def test_oserror_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("subprocess.run", side_effect=OSError("permission denied")),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-shellcheck.sh")
            script.write_text("#!/bin/bash\n")
            script.chmod(0o644)
            findings = run_analyzers(ChangedFiles(shell=["review.sh"]), "/dev/null", tmpdir)
        captured = capsys.readouterr()
        assert findings == []
        assert "[ai-pr-review] WARNING:" in captured.err

    def test_non_json_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _normalise_output("not json at all", "my-analyzer")
        captured = capsys.readouterr()
        assert result == []
        assert "[ai-pr-review] WARNING:" in captured.err
