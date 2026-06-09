"""Tests for the native tflint analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.tflint import _run_tflint
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "tflint"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(tf_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=tf_files, terraform=tf_files)


class TestRunTflintGuards:
    def test_no_terraform_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_tflint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value=None):
            result = _run_tflint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_tflint(cf, Path("/dev/null"))
        assert "tflint not found" in caplog.text


class TestRunTflintFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            return _run_tflint(cf, Path("/dev/null"))

    def test_error_severity(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("tflint-error.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "High"
        assert f.confidence == 90
        assert f.source == "tflint"
        assert "aws_instance_invalid_type" in f.finding
        assert f.line == 8

    def test_warning_severity(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("tflint-warning.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "Medium"
        assert "terraform_deprecated_interpolation" in findings[0].finding

    def test_empty_issues_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("tflint-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("tflint-malformed.json")
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            result = _run_tflint(cf, Path("/dev/null"))
        assert result == []
        assert "non-JSON" in caplog.text

    def test_rule_link_used_as_remediation(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("tflint-error.json", tmp_path)
        assert "github.com/terraform-linters" in findings[0].remediation

    def test_no_rule_link_uses_default_remediation(self, tmp_path: Path) -> None:
        payload = json.dumps({"issues": [{
            "rule": {"name": "some_rule", "severity": "error", "link": ""},
            "message": "some issue",
            "range": {"filename": "main.tf", "start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 5}},
        }], "errors": []})
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert "tflint-ruleset-aws" in findings[0].remediation

    def test_notice_severity_maps_to_low(self, tmp_path: Path) -> None:
        payload = json.dumps({"issues": [{
            "rule": {"name": "some_rule", "severity": "notice", "link": ""},
            "message": "notice message",
            "range": {"filename": "main.tf", "start": {"line": 3, "column": 1}, "end": {"line": 3, "column": 5}},
        }], "errors": []})
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert findings[0].severity == "Low"

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="tflint", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_tflint(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run", side_effect=OSError("not found")),
        ):
            findings = _run_tflint(cf, Path("/dev/null"))
        assert findings == []

    def test_exit_code_2_logs_warning_and_skips(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="init error")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_errors_field_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        payload = json.dumps({"issues": [], "errors": [{"message": "plugin load failed"}]})
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert findings == []
        assert "plugin load failed" in caplog.text

    def test_absolute_path_dir_constructs_correct_file_path(self, tmp_path: Path) -> None:
        subdir = tmp_path / "vpc"
        subdir.mkdir()
        tf = subdir / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        payload = json.dumps({"issues": [{
            "rule": {"name": "some_rule", "severity": "error", "link": ""},
            "message": "bad",
            "range": {"filename": "main.tf", "start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 5}},
        }], "errors": []})
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].file == str(subdir / "main.tf")

    def test_dir_prefix_prepended_for_subdirectory(self, tmp_path: Path) -> None:
        subdir = tmp_path / "modules" / "vpc"
        subdir.mkdir(parents=True)
        tf = subdir / "main.tf"
        tf.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf)])
        payload = json.dumps({"issues": [{
            "rule": {"name": "aws_instance_invalid_type", "severity": "error", "link": ""},
            "message": "bad type",
            "range": {"filename": "main.tf", "start": {"line": 5, "column": 1}, "end": {"line": 5, "column": 5}},
        }], "errors": []})
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert findings[0].file.endswith("/main.tf")
        assert "main.tf" in findings[0].file
        assert findings[0].file != "main.tf"

    def test_multiple_dirs_run_separately(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        tf_a = dir_a / "main.tf"
        tf_b = dir_b / "main.tf"
        tf_a.write_text('resource "x" "y" {}\n')
        tf_b.write_text('resource "x" "y" {}\n')
        cf = _make_cf([str(tf_a), str(tf_b)])
        payload = json.dumps({"issues": [{
            "rule": {"name": "some_rule", "severity": "error", "link": ""},
            "message": "msg",
            "range": {"filename": "main.tf", "start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 5}},
        }], "errors": []})
        with (
            patch("ai_pr_review.analyzers.native.tflint.shutil.which", return_value="/usr/bin/tflint"),
            patch("ai_pr_review.analyzers.native.tflint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_tflint(cf, Path("/dev/null"))
        assert mock_run.call_count == 2
        assert len(findings) == 2


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_tflint_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["main.tf"], terraform=["main.tf"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "tflint" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null", str(tmp_path))

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_tflint_skipped_when_no_terraform_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("tflint", "run-tflint.sh", ["terraform"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null", str(tmp_path))

        assert not called
