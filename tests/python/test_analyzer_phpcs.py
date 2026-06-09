"""Tests for the native phpcs analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.phpcs import _run_phpcs
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "phpcs"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(php_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=php_files, php=php_files)


class TestRunPhpcs:
    def test_no_php_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_phpcs(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value=None):
            result = _run_phpcs(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_phpcs(cf, Path("/dev/null"))
        assert "phpcs not found" in caplog.text

    def test_only_php_extension_files_passed(self, tmp_path: Path) -> None:
        php = tmp_path / "code.php"
        php.write_text("<?php\n")
        txt = tmp_path / "readme.txt"
        txt.write_text("hello\n")
        cf = _make_cf([str(php), str(txt)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")
            _run_phpcs(cf, Path("/dev/null"))
        # Only phpcs -i call (for standard detection) and the main call
        # The main phpcs call should include only php, not txt
        main_call = mock_run.call_args_list[-1]
        assert str(php) in main_call[0][0]
        assert str(txt) not in main_call[0][0]


class TestRunPhpcsFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            # First call = phpcs -i (standard detection), second = main scan
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Drupal,DrupalPractice", stderr=""),
                MagicMock(returncode=1, stdout=fixture, stderr=""),
            ]
            return _run_phpcs(cf, Path("/dev/null"))

    def test_warning_finding_maps_to_medium(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-warning.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 90
        assert f.source == "phpcs"
        assert "Generic.Files.LineLength.TooLong" in f.finding
        assert f.line == 15

    def test_error_finding_maps_to_high(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-error.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "Generic.PHP.UpperCaseConstant" in findings[0].finding

    def test_empty_files_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("phpcs-malformed.json")
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=1, stdout=fixture, stderr=""),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_path_prefix_stripped(self, tmp_path: Path) -> None:
        import os as _os
        pwd = _os.getcwd()
        payload = json.dumps({
            "totals": {"errors": 1, "warnings": 0, "fixable": 0},
            "files": {
                f"{pwd}/src/module.php": {
                    "errors": 1, "warnings": 0,
                    "messages": [{
                        "message": "uppercase expected", "source": "Generic.PHP.UpperCaseConstant",
                        "severity": 5, "type": "ERROR", "line": 5, "column": 1, "fixable": False,
                    }],
                },
            },
        })
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=1, stdout=payload, stderr=""),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings[0].file == "src/module.php"

    def test_exitcode_2_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=2, stdout="", stderr="fatal error"),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                sp.TimeoutExpired(cmd="phpcs", timeout=120),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                OSError("not found"),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []

    def test_drupal_standard_used_when_available(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="The following coding standards are installed: Drupal, DrupalPractice, PSR12", stderr=""),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        main_call_args = mock_run.call_args_list[-1][0][0]
        assert "--standard=Drupal,DrupalPractice" in main_call_args

    def test_psr12_standard_when_drupal_unavailable(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12, PEAR", stderr=""),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        main_call_args = mock_run.call_args_list[-1][0][0]
        assert "--standard=PSR12" in main_call_args


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_phpcs_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["module.php"], php=["module.php"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "phpcs" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null", str(tmp_path))

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_phpcs_skipped_when_no_php_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("phpcs", "run-phpcs.sh", ["php"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null", str(tmp_path))

        assert not called
