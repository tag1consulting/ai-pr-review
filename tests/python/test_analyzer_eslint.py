"""Tests for the native eslint analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.eslint import _has_eslint_config, _run_eslint
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eslint"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(js_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=js_files, js_ts=js_files)


class TestHasEslintConfig:
    def test_detects_eslintrc_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        assert _has_eslint_config()

    def test_detects_eslint_config_js(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "eslint.config.js").write_text("module.exports = [];\n")
        assert _has_eslint_config()

    def test_no_config_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert not _has_eslint_config()


class TestRunEslintGuards:
    def test_no_js_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_eslint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=None),
        ):
            result = _run_eslint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_eslint(cf, Path("/dev/null"))
        assert "eslint not found" in caplog.text

    def test_no_config_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
        ):
            result = _run_eslint(cf, Path("/dev/null"))
        assert result == []


class TestRunEslintFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            return _run_eslint(cf, Path("/dev/null"))

    def test_double_dash_precedes_target_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Argument-injection guard: target files must follow a literal "--".
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            _run_eslint(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--" in call_args
        assert call_args.index("--") < call_args.index(str(f))

    def test_error_finding_maps_to_high(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        findings = self._run_with_fixture("eslint-error.json", tmp_path, monkeypatch)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "High"
        assert f.confidence == 90
        assert f.source == "eslint"
        assert "no-unused-vars" in f.finding
        assert f.line == 8

    def test_warning_finding_maps_to_medium(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        findings = self._run_with_fixture("eslint-warning.json", tmp_path, monkeypatch)
        assert len(findings) == 1
        assert findings[0].severity == "Medium"
        assert "no-console" in findings[0].finding
        assert findings[0].line == 22

    def test_empty_results_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        findings = self._run_with_fixture("eslint-empty.json", tmp_path, monkeypatch)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        fixture = _load_fixture("eslint-malformed.json")
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            result = _run_eslint(cf, Path("/dev/null"))
        assert result == []
        assert "non-JSON" in caplog.text

    def test_cwd_prefix_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        cwd = os.getcwd()
        payload = json.dumps([{
            "filePath": f"{cwd}/src/app.ts",
            "messages": [{"ruleId": "no-unused-vars", "severity": 2, "message": "unused", "line": 1}],
        }])
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_eslint(cf, Path("/dev/null"))
        assert findings[0].file == "src/app.ts"

    def test_remediation_includes_rule_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        findings = self._run_with_fixture("eslint-error.json", tmp_path, monkeypatch)
        assert "eslint.org/docs/rules/no-unused-vars" in findings[0].remediation

    def test_exitcode_2_returns_empty_with_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="config error")
            findings = _run_eslint(cf, Path("/dev/null"))
        assert findings == []
        assert "fatal error" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="eslint", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_eslint(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run", side_effect=OSError("not found")),
        ):
            findings = _run_eslint(cf, Path("/dev/null"))
        assert findings == []

    def test_messages_without_ruleId_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        payload = json.dumps([{
            "filePath": "/repo/app.ts",
            "messages": [
                {"ruleId": None, "severity": 2, "message": "parsing error", "line": 1},
                {"ruleId": "semi", "severity": 2, "message": "Missing semicolon.", "line": 5},
            ],
        }])
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=False),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_eslint(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert "semi" in findings[0].finding

    def test_no_warn_ignored_flag_added_when_supported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".eslintrc.json").write_text('{"rules":{}}\n')
        f = tmp_path / "app.ts"
        f.write_text("const x = 1;\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.eslint._find_eslint_bin", return_value=["/usr/bin/eslint"]),
            patch("ai_pr_review.analyzers.native.eslint._supports_no_warn_ignored", return_value=True),
            patch("ai_pr_review.analyzers.native.eslint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            _run_eslint(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--no-warn-ignored" in call_args


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_eslint_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["app.ts"], js_ts=["app.ts"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "eslint" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_eslint_skipped_when_no_js_ts_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("eslint", ["js_ts"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
