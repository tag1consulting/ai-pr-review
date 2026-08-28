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
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path, returncode: int = 1) -> list:
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
                MagicMock(returncode=returncode, stdout=fixture, stderr=""),
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

    def test_category_is_lint(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-warning.json", tmp_path)
        assert findings[0].category == "lint"

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

    def test_exitcode_2_with_real_output_parses_findings(self, tmp_path: Path) -> None:
        # phpcs 4.x exit code 2 means "non-fixable violations found" (a normal
        # successful run), not a fatal error. PHPCSStandards/PHP_CodeSniffer#184
        # redesigned exit codes in 4.0 as a bitmask: 1=fixable, 2=non-fixable,
        # 3=both. Verified empirically against phpcs 4.0.4 this session.
        findings = self._run_with_fixture("phpcs-warning.json", tmp_path, returncode=2)
        assert len(findings) == 1
        assert findings[0].severity == "Medium"

    def test_exitcode_3_mixed_fixable_and_nonfixable_parses_findings(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-error.json", tmp_path, returncode=3)
        assert len(findings) == 1
        assert findings[0].severity == "High"

    def test_process_error_exitcode_returns_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Genuine fatal/config errors use ExitCode::PROCESS_ERROR (16) or
        # ::REQUIREMENTS_NOT_MET (64) in phpcs 4.x — these never overlap the
        # 0-3 violation-bitmask range, so they're the real "something broke"
        # signal, not exit code 2.
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
                MagicMock(returncode=16, stdout="", stderr="fatal error"),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 16" in caplog.text

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

    def test_standard_detection_timeout_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
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
                sp.TimeoutExpired(cmd="phpcs", timeout=10),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        assert "phpcs -i timed out" in caplog.text

    def test_standard_detection_oserror_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                OSError("bad interpreter"),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        assert "phpcs -i failed" in caplog.text

    def test_psr12_standard_when_drupal_unavailable(self, tmp_path: Path) -> None:
        # The PSR12 fallback is upgraded to a temp-file ruleset (PSR12 plus
        # the Squiz docblock sniff) rather than the bare "PSR12" string —
        # phpcs's --standard flag cannot mix a named standard with a bare
        # sniff code, verified empirically this session — so the argument
        # passed to phpcs is a filesystem path, not the literal "PSR12".
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
        standard_flag = next(a for a in main_call_args if a.startswith("--standard="))
        ruleset_path = standard_flag.removeprefix("--standard=")
        assert ruleset_path.endswith(".xml")
        # The remediation message shown to reviewers still says "PSR12" (the
        # human-readable label), not the temp-file path.
        assert "--standard=PSR12" not in main_call_args

    def test_psr12_ruleset_combines_psr12_and_docblock_sniff(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        written_ruleset = {}

        def _capture_run(args: list[str], **kwargs: object) -> MagicMock:
            standard_flag = next((a for a in args if a.startswith("--standard=")), None)
            if standard_flag and standard_flag.removeprefix("--standard=").endswith(".xml"):
                written_ruleset["content"] = Path(standard_flag.removeprefix("--standard=")).read_text()
            if args[0:2] == ["phpcs", "-i"]:
                return MagicMock(returncode=0, stdout="PSR12", stderr="")
            return MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")

        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run", side_effect=_capture_run),
        ):
            _run_phpcs(cf, Path("/dev/null"))
        assert '<rule ref="PSR12"/>' in written_ruleset["content"]
        assert '<rule ref="Squiz.Commenting.FunctionComment"/>' in written_ruleset["content"]

    def test_drupal_standard_does_not_use_temp_ruleset(self, tmp_path: Path) -> None:
        # The docblock-sniff upgrade is scoped to the PSR12 fallback only —
        # Drupal repos already get equivalent coverage via
        # Drupal.Commenting.FunctionComment, so the Drupal branch is untouched.
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Drupal, DrupalPractice", stderr=""),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        main_call_args = mock_run.call_args_list[-1][0][0]
        assert "--standard=Drupal,DrupalPractice" in main_call_args


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
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_phpcs_skipped_when_no_php_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("phpcs", ["php"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
