"""Tests for the native semgrep analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.semgrep import _resolve_config, _run_semgrep
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "semgrep"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=files)


class TestResolveConfig:
    def test_semgrep_rules_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEMGREP_RULES", "p/ci")
        monkeypatch.chdir(tmp_path)
        assert _resolve_config() == ["--config", "p/ci"]

    def test_semgrep_dir_with_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SEMGREP_RULES", raising=False)
        semgrep_dir = tmp_path / ".semgrep"
        semgrep_dir.mkdir()
        rule = semgrep_dir / "rules.yml"
        rule.write_text("rules: []\n")
        result = _resolve_config()
        assert result == ["--config", ".semgrep/rules.yml"]

    def test_semgrep_yml_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SEMGREP_RULES", raising=False)
        (tmp_path / "semgrep.yml").write_text("rules: []\n")
        assert _resolve_config() == ["--config", "semgrep.yml"]

    def test_auto_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SEMGREP_RULES", raising=False)
        assert _resolve_config() == ["--config=auto"]

    def test_env_var_takes_priority_over_semgrep_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SEMGREP_RULES", "p/security-audit")
        semgrep_dir = tmp_path / ".semgrep"
        semgrep_dir.mkdir()
        (semgrep_dir / "rules.yml").write_text("rules: []\n")
        assert _resolve_config() == ["--config", "p/security-audit"]


class TestRunSemgrep:
    def test_no_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_semgrep(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value=None):
            result = _run_semgrep(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_semgrep(cf, Path("/dev/null"))
        assert "semgrep not found" in caplog.text


class TestRunSemgrepFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            return _run_semgrep(cf, Path("/dev/null"))

    def test_warning_finding_maps_to_medium(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("semgrep-warning.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 90
        assert f.source == "semgrep"
        assert "python.lang.maintainability.useless-comparison" in f.finding
        assert f.line == 10

    def test_error_finding_maps_to_high(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("semgrep-error.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "subprocess-shell-true" in findings[0].finding

    def test_empty_results_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("semgrep-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("semgrep-malformed.json")
        f = tmp_path / "app.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_reference_present_in_remediation(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("semgrep-error.json", tmp_path)
        assert "https://semgrep.dev" in findings[0].remediation

    def test_no_reference_uses_check_id(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("semgrep-warning.json", tmp_path)
        assert "python.lang.maintainability.useless-comparison" in findings[0].remediation

    def test_info_severity_maps_to_low(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": [{
                "check_id": "python.lang.style.info-check",
                "path": "a.py", "start": {"line": 1, "col": 1}, "end": {"line": 1, "col": 5},
                "extra": {"severity": "INFO", "message": "style note", "metadata": {}},
            }],
            "errors": [],
        })
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].severity == "Low"

    def test_empty_stdout_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="network error")
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert findings == []
        assert "no output" in caplog.text

    def test_non_zero_non_one_returncode_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="fatal error")
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="semgrep", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run", side_effect=OSError("not found")),
            caplog.at_level("WARNING"),
        ):
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert findings == []

    def test_non_dict_items_skipped(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": [
                "not-a-dict",
                {"check_id": "rule.x", "path": "a.py", "start": {"line": 1, "col": 1},
                 "end": {"line": 1, "col": 5}, "extra": {"severity": "WARNING", "message": "x", "metadata": {}}},
            ],
            "errors": [],
        })
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.semgrep.shutil.which", return_value="/usr/bin/semgrep"),
            patch("ai_pr_review.analyzers.native.semgrep._resolve_config", return_value=["--config=auto"]),
            patch("ai_pr_review.analyzers.native.semgrep.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_semgrep(cf, Path("/dev/null"))
        assert len(findings) == 1


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_semgrep_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["app.py"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "semgrep" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"
