"""Tests for the native ruff analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.ruff import _run_ruff
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "ruff"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(py_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=py_files, python=py_files)


class TestRunRuff:
    def test_no_python_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_ruff(cf, Path("/dev/null"))
        assert result == []

    def test_only_existing_files_are_scanned(self, tmp_path: Path) -> None:
        existing = tmp_path / "real.py"
        existing.write_text("import os\n")
        cf = _make_cf([str(existing), "/nonexistent/ghost.py"])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            _run_ruff(cf, Path("/dev/null"))
        assert mock_run.call_count == 1
        call_args = mock_run.call_args[0][0]
        assert str(existing) in call_args
        assert "/nonexistent/ghost.py" not in call_args

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value=None):
            result = _run_ruff(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_ruff(cf, Path("/dev/null"))
        assert "ruff not found" in caplog.text


class TestRunRuffFindings:
    def _mock_run(self, output: str, returncode: int = 0) -> MagicMock:
        m = MagicMock()
        m.return_value = MagicMock(returncode=returncode, stdout=output, stderr="")
        return m

    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "file.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            return _run_ruff(cf, Path("/dev/null"))

    def test_warning_finding_maps_to_medium(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("ruff-warning.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 90
        assert f.source == "ruff"
        assert "W291" in f.finding
        assert f.line == 22

    def test_error_finding_maps_to_high(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("ruff-error.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "F401" in findings[0].finding

    def test_category_is_lint(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("ruff-warning.json", tmp_path)
        assert findings[0].category == "lint"

    def test_empty_output_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("ruff-empty.json", tmp_path)
        assert findings == []

    def test_url_present_uses_url_remediation(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("ruff-warning.json", tmp_path)
        assert findings[0].remediation == "See https://docs.astral.sh/ruff/rules/trailing-whitespace"

    def test_no_url_uses_fallback_remediation(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("ruff-no-url.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].remediation == "See https://docs.astral.sh/ruff/rules/I001"
        assert findings[0].severity == "Low"

    def test_f_prefix_maps_to_high(self, tmp_path: Path) -> None:
        payload = json.dumps([{
            "code": "F841", "filename": "a.py",
            "location": {"row": 5, "column": 1}, "end_location": {"row": 5, "column": 5},
            "message": "Local variable `x` is assigned to but never used",
            "url": "https://docs.astral.sh/ruff/rules/unused-variable",
        }])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings[0].severity == "High"

    def test_e_prefix_maps_to_high(self, tmp_path: Path) -> None:
        payload = json.dumps([{
            "code": "E501", "filename": "a.py",
            "location": {"row": 1, "column": 80}, "end_location": {"row": 1, "column": 90},
            "message": "Line too long (90 > 88)",
            "url": None,
        }])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings[0].severity == "High"

    def test_w_prefix_maps_to_medium(self, tmp_path: Path) -> None:
        payload = json.dumps([{
            "code": "W605", "filename": "a.py",
            "location": {"row": 2, "column": 1}, "end_location": {"row": 2, "column": 5},
            "message": "Invalid escape sequence: `\\d`",
            "url": None,
        }])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings[0].severity == "Medium"

    def test_c_prefix_maps_to_medium(self, tmp_path: Path) -> None:
        payload = json.dumps([{
            "code": "C901", "filename": "a.py",
            "location": {"row": 3, "column": 1}, "end_location": {"row": 3, "column": 5},
            "message": "Function is too complex",
            "url": None,
        }])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings[0].severity == "Medium"

    def test_other_prefix_maps_to_low(self, tmp_path: Path) -> None:
        payload = json.dumps([{
            "code": "N802", "filename": "a.py",
            "location": {"row": 4, "column": 1}, "end_location": {"row": 4, "column": 5},
            "message": "Function name should be lowercase",
            "url": None,
        }])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings[0].severity == "Low"

    def test_path_prefix_stripped(self, tmp_path: Path) -> None:
        import os as _os
        workspace = "/workspace/myrepo"
        payload = json.dumps([{
            "code": "F401", "filename": f"{workspace}/app/views.py",
            "location": {"row": 1, "column": 1}, "end_location": {"row": 1, "column": 5},
            "message": "'os' imported but unused", "url": None,
        }])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
            patch.dict(_os.environ, {"GITHUB_WORKSPACE": workspace}),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings[0].file == "app/views.py"

    def test_non_list_json_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"unexpected": "object"}', stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings == []
        assert "unexpected output structure" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="ruff", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run", side_effect=OSError("not found")),
            caplog.at_level("WARNING"),
        ):
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="not-json", stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_empty_stdout_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings == []

    def test_non_zero_non_one_returncode_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="bad invocation")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_non_dict_items_skipped(self, tmp_path: Path) -> None:
        payload = json.dumps([
            "not-a-dict",
            {"code": "F401", "filename": "a.py", "location": {"row": 1, "column": 1},
             "end_location": {"row": 1, "column": 5}, "message": "unused", "url": None},
        ])
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.ruff.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.ruff.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_ruff(cf, Path("/dev/null"))
        assert len(findings) == 1


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_ruff_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["main.py"], python=["main.py"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "ruff" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_ruff_skipped_when_no_python_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("ruff", ["python"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
