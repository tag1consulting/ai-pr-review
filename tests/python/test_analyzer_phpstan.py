"""Tests for the native phpstan analyzer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.phpstan import _run_phpstan
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "phpstan"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(php_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=php_files, php=php_files)


class TestRunPhpstanGuards:
    def test_no_php_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_phpstan(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value=None):
            result = _run_phpstan(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_phpstan(cf, Path("/dev/null"))
        assert "phpstan not found" in caplog.text

    def test_invalid_level_falls_back_to_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        monkeypatch.setenv("PHPSTAN_LEVEL", "bogus")
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--level=3" in call_args

    def test_valid_level_env_var_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        monkeypatch.setenv("PHPSTAN_LEVEL", "7")
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--level=7" in call_args

    def test_double_dash_precedes_target_files(self, tmp_path: Path) -> None:
        # Argument-injection guard: every target file must follow a literal
        # "--" so a dash-leading filename cannot be parsed as a phpstan flag.
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--" in call_args
        assert call_args.index("--") < call_args.index(str(f))


class TestRunPhpstanFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            return _run_phpstan(cf, Path("/dev/null"))

    def test_error_finding_maps_to_high(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpstan-error.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "High"
        assert f.confidence == 85
        assert f.source == "phpstan"
        assert "getTitle" in f.finding
        assert f.line == 55

    def test_empty_files_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpstan-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("phpstan-malformed.json")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            result = _run_phpstan(cf, Path("/dev/null"))
        assert result == []
        assert "non-JSON" in caplog.text

    def test_cwd_prefix_stripped(self, tmp_path: Path) -> None:
        cwd = os.getcwd()
        payload = json.dumps({
            "totals": {"errors": 1, "file_errors": 1},
            "files": {
                f"{cwd}/src/MyService.php": {
                    "errors": 1,
                    "messages": [{"message": "Type error", "line": 10, "ignorable": True}],
                }
            },
            "errors": [],
        })
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings[0].file == "src/MyService.php"

    def test_exitcode_2_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="config error")
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="phpstan", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run", side_effect=OSError("not found")),
        ):
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings == []

    def test_config_present_skips_level_arg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "phpstan.neon").write_text("parameters:\n  level: 5\n")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert not any(a.startswith("--level=") for a in call_args)

    def test_drupal_autoload_added_when_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        vendor_drupal = tmp_path / "vendor" / "mglaman" / "phpstan-drupal"
        vendor_drupal.mkdir(parents=True)
        (tmp_path / "vendor" / "autoload.php").write_text("<?php\n")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--autoload-file=vendor/autoload.php" in call_args

    def test_remediation_references_phpstan(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpstan-error.json", tmp_path)
        assert "phpstan.org" in findings[0].remediation


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_phpstan_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["MyService.php"], php=["MyService.php"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "phpstan" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null", str(tmp_path))

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_phpstan_skipped_when_no_php_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("phpstan", "run-phpstan.sh", ["php"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null", str(tmp_path))

        assert not called
