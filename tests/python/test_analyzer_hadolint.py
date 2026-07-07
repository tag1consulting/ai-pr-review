"""Tests for the native hadolint analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.hadolint import _is_dockerfile, _run_hadolint
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "hadolint"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(dockerfile_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=dockerfile_files, dockerfile=dockerfile_files)


class TestIsDockerfile:
    def test_dockerfile_bare(self) -> None:
        assert _is_dockerfile("Dockerfile")

    def test_dockerfile_in_subdir(self) -> None:
        assert _is_dockerfile("services/api/Dockerfile")

    def test_dockerfile_with_extension(self) -> None:
        assert _is_dockerfile("Dockerfile.prod")

    def test_dockerfile_suffix(self) -> None:
        assert _is_dockerfile("app.dockerfile")

    def test_non_dockerfile_rejected(self) -> None:
        assert not _is_dockerfile("main.py")
        assert not _is_dockerfile("requirements.txt")


class TestRunHadolint:
    def test_no_dockerfile_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_hadolint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value=None):
            result = _run_hadolint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_hadolint(cf, Path("/dev/null"))
        assert "hadolint not found" in caplog.text

    def test_only_existing_files_are_scanned(self, tmp_path: Path) -> None:
        existing = tmp_path / "Dockerfile"
        existing.write_text("FROM ubuntu\n")
        cf = _make_cf([str(existing), "/nonexistent/Dockerfile"])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            _run_hadolint(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert str(existing) in call_args
        assert "/nonexistent/Dockerfile" not in call_args


class TestRunHadolintFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            return _run_hadolint(cf, Path("/dev/null"))

    def test_warning_finding_maps_to_medium(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("hadolint-warning.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 90
        assert f.source == "hadolint"
        assert "DL3009" in f.finding
        assert f.line == 3

    def test_error_finding_maps_to_high(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("hadolint-error.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "DL3008" in findings[0].finding

    def test_info_finding_maps_to_low(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("hadolint-info.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "Low"
        assert "DL3006" in findings[0].finding

    def test_category_is_lint(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("hadolint-warning.json", tmp_path)
        assert findings[0].category == "lint"

    def test_empty_output_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("hadolint-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        fixture = _load_fixture("hadolint-malformed.json")
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_remediation_format(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("hadolint-error.json", tmp_path)
        assert findings[0].remediation == "See https://github.com/hadolint/hadolint/wiki/DL3008"

    def test_style_level_maps_to_low(self, tmp_path: Path) -> None:
        payload = json.dumps([{
            "file": "Dockerfile", "line": 2, "column": 1,
            "level": "style", "code": "DL3040",
            "message": "Use SHELL to change the default shell.",
        }])
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].severity == "Low"

    def test_returncode_one_treated_as_findings(self, tmp_path: Path) -> None:
        payload = json.dumps([{"file": "Dockerfile", "line": 1, "column": 1,
                                "level": "warning", "code": "DL3009", "message": "test"}])
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert len(findings) == 1

    def test_non_zero_non_one_returncode_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="error!")
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="hadolint", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run", side_effect=OSError("not found")),
            caplog.at_level("WARNING"),
        ):
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert findings == []

    def test_non_list_json_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"unexpected": "object"}', stderr="")
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert findings == []
        assert "unexpected output structure" in caplog.text

    def test_non_dict_items_skipped(self, tmp_path: Path) -> None:
        payload = json.dumps([
            "not-a-dict",
            {"file": "Dockerfile", "line": 1, "column": 1,
             "level": "warning", "code": "DL3009", "message": "test"},
        ])
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.hadolint.shutil.which", return_value="/usr/bin/hadolint"),
            patch("ai_pr_review.analyzers.native.hadolint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_hadolint(cf, Path("/dev/null"))
        assert len(findings) == 1


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_hadolint_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["Dockerfile"], dockerfile=["Dockerfile"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "hadolint" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_hadolint_skipped_when_no_dockerfile_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("hadolint", ["dockerfile"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
