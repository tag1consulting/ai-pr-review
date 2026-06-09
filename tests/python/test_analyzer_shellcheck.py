"""Tests for the native shellcheck analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.shellcheck import _run_shellcheck, _scan_file
from ai_pr_review.manifest import ChangedFiles

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "shellcheck"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(shell_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=shell_files, shell=shell_files)


# ---------------------------------------------------------------------------
# _run_shellcheck — top-level entry point
# ---------------------------------------------------------------------------


class TestRunShellcheck:
    def test_no_shell_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_shellcheck(cf, Path("/dev/null"))
        assert result == []

    def test_only_existing_files_are_scanned(self, tmp_path: Path) -> None:
        existing = tmp_path / "real.sh"
        existing.write_text("#!/bin/bash\necho $1\n")
        cf = _make_cf([str(existing), "/nonexistent/ghost.sh"])
        with (
            patch("ai_pr_review.analyzers.native.shellcheck.shutil.which", return_value="/usr/bin/shellcheck"),
            patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout='{"comments":[]}', stderr="")
            _run_shellcheck(cf, Path("/dev/null"))
        # Only the existing file should have been scanned
        assert mock_run.call_count == 1
        assert str(existing) in mock_run.call_args[0][0]

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "test.sh"
        f.write_text("#!/bin/bash\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.shellcheck.shutil.which", return_value=None):
            result = _run_shellcheck(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "test.sh"
        f.write_text("#!/bin/bash\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.shellcheck.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_shellcheck(cf, Path("/dev/null"))
        assert "shellcheck not found" in caplog.text


# ---------------------------------------------------------------------------
# _scan_file — per-file scanner
# ---------------------------------------------------------------------------


class TestScanFile:
    def _mock_run(self, output: str, returncode: int = 1) -> MagicMock:
        m = MagicMock()
        m.return_value = MagicMock(returncode=returncode, stdout=output, stderr="")
        return m

    def test_warning_finding(self) -> None:
        fixture = _load_fixture("shellcheck-warning.json")
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            findings = _scan_file("test.sh")
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 95
        assert f.source == "shellcheck"
        assert f.file == "test.sh"
        assert "SC2086" in f.finding
        assert "shellcheck.net/wiki/SC2086" in f.remediation

    def test_error_finding_maps_to_high(self) -> None:
        fixture = _load_fixture("shellcheck-error.json")
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            findings = _scan_file("test.sh")
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "SC2104" in findings[0].finding

    def test_empty_comments_returns_empty(self) -> None:
        fixture = _load_fixture("shellcheck-empty.json")
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            findings = _scan_file("clean.sh")
        assert findings == []

    def test_malformed_json_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("shellcheck-malformed.json")
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            with caplog.at_level("WARNING"):
                findings = _scan_file("test.sh")
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_empty_stdout_returns_empty(self) -> None:
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            findings = _scan_file("empty.sh")
        assert findings == []

    def test_timeout_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        with (
            patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="shellcheck", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _scan_file("slow.sh")
        assert findings == []
        assert "shellcheck timed out" in caplog.text

    def test_oserror_returns_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run",
                   side_effect=OSError("not found")), caplog.at_level("WARNING"):
            findings = _scan_file("missing.sh")
        assert findings == []

    def test_style_level_maps_to_low(self) -> None:
        payload = json.dumps({"comments": [
            {"file": "f.sh", "line": 1, "level": "style", "code": 2148,
             "message": "Tips depend on target shell and target audience."}
        ]})
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _scan_file("f.sh")
        assert len(findings) == 1
        assert findings[0].severity == "Low"

    def test_info_level_maps_to_low(self) -> None:
        payload = json.dumps({"comments": [
            {"file": "f.sh", "line": 5, "level": "info", "code": 2166,
             "message": "Prefer [ p ] || [ q ] as [ p -o q ] is not well defined."}
        ]})
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _scan_file("f.sh")
        assert len(findings) == 1
        assert findings[0].severity == "Low"

    def test_finding_text_format(self) -> None:
        payload = json.dumps({"comments": [
            {"file": "f.sh", "line": 3, "level": "warning", "code": 2086,
             "message": "Double quote to prevent globbing."}
        ]})
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _scan_file("f.sh")
        assert findings[0].finding == "SC2086: Double quote to prevent globbing."
        assert findings[0].remediation == "See https://www.shellcheck.net/wiki/SC2086"

    def test_non_dict_comment_items_skipped(self) -> None:
        payload = json.dumps({"comments": [
            "not-a-dict",
            {"file": "f.sh", "line": 1, "level": "warning", "code": 2086, "message": "x"},
        ]})
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _scan_file("f.sh")
        assert len(findings) == 1

    def test_line_zero_normalized_to_none(self) -> None:
        """shellcheck emits line=0 for file-scope parse errors; must not drop the finding."""
        payload = json.dumps({"comments": [
            {"file": "f.sh", "line": 0, "level": "error", "code": 1073,
             "message": "Couldn't parse this 'if' expression."}
        ]})
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _scan_file("f.sh")
        assert len(findings) == 1
        assert findings[0].line is None
        assert findings[0].severity == "High"

    def test_non_zero_non_one_returncode_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("ai_pr_review.analyzers.native.shellcheck.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="bad invocation")
            with caplog.at_level("WARNING"):
                findings = _scan_file("broken.sh")
        assert findings == []
        assert "exited 2" in caplog.text


# ---------------------------------------------------------------------------
# Bridge integration — native_fn dispatched instead of bash script
# ---------------------------------------------------------------------------


class TestBridgeIntegration:
    def test_shellcheck_uses_native_fn(self, tmp_path: Path) -> None:
        """run_analyzers dispatches to native_fn when set, not bash script."""
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["review.sh"], shell=["review.sh"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        original = bridge._ANALYZERS
        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "shellcheck" else spec
            for spec in original
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            run_analyzers(cf, "/dev/null", str(tmp_path))

        assert called, "Native fn was not called"

    def test_native_fn_skipped_when_ineligible(self, tmp_path: Path) -> None:
        """native_fn is not called when required_file_types are absent from ChangedFiles."""
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            run_analyzers(ChangedFiles(), "/dev/null", str(tmp_path))

        assert not called, "Native fn was called despite no eligible files"

    def test_native_fn_exception_is_caught(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Unhandled exception in native_fn must not abort run_analyzers."""
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        def exploding_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            raise RuntimeError("unexpected crash")

        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"], exploding_native)
        cf = ChangedFiles(shell=["review.sh"])
        with patch.object(bridge, "_ANALYZERS", [spec]):
            findings = run_analyzers(cf, "/dev/null", str(tmp_path))

        assert findings == []
        captured = capsys.readouterr()
        assert "RuntimeError" in captured.err
