"""Tests for the native golangci-lint analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.golangci_lint import _run_golangci_lint
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "golangci"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(go_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=go_files, go=go_files)


class TestRunGolangciLintGuards:
    def test_no_go_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_golangci_lint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value=None):
            result = _run_golangci_lint(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_golangci_lint(cf, Path("/dev/null"))
        assert "golangci-lint not found" in caplog.text

    def test_no_go_mod_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            caplog.at_level("WARNING"),
        ):
            result = _run_golangci_lint(cf, Path("/dev/null"))
        assert result == []
        assert "go.mod" in caplog.text


class TestRunGolangciLintFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            return _run_golangci_lint(cf, Path("/dev/null"))

    def test_medium_finding(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("golangci-medium.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 90
        assert f.source == "golangci-lint"
        assert "gofmt" in f.finding
        assert f.line == 5

    def test_high_severity_linters(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("golangci-high.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "errcheck" in findings[0].finding

    def test_category_is_lint(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("golangci-medium.json", tmp_path)
        assert findings[0].category == "lint"

    def test_empty_issues_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("golangci-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("golangci-malformed.json")
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            result = _run_golangci_lint(cf, Path("/dev/null"))
        assert result == []
        assert "non-JSON" in caplog.text

    def test_non_list_issues_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("golangci-non-list-issues.json")
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            result = _run_golangci_lint(cf, Path("/dev/null"))
        assert result == []
        assert "not a list" in caplog.text

    def test_empty_stdout_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="compilation error")
            findings = _run_golangci_lint(cf, Path("/dev/null"))
        assert findings == []
        assert "no output" in caplog.text

    def test_non_zero_returncode_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="fatal")
            findings = _run_golangci_lint(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="golangci-lint", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_golangci_lint(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run", side_effect=OSError("not found")),
        ):
            findings = _run_golangci_lint(cf, Path("/dev/null"))
        assert findings == []

    def test_high_severity_linter_names(self, tmp_path: Path) -> None:
        """govet and staticcheck also map to High."""
        for linter in ("govet", "staticcheck"):
            payload = json.dumps({
                "Issues": [{
                    "FromLinter": linter,
                    "Text": "some issue",
                    "Pos": {"Filename": "main.go", "Line": 1, "Column": 1},
                }]
            })
            go_mod = tmp_path / "go.mod"
            go_mod.write_text("module example.com/test\n\ngo 1.21\n")
            go_file = tmp_path / "main.go"
            go_file.write_text("package main\n")
            cf = _make_cf([str(go_file)])
            with (
                patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
                patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
                findings = _run_golangci_lint(cf, Path("/dev/null"))
            assert findings[0].severity == "High", f"{linter} should map to High"

    def test_remediation_includes_linter_name(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("golangci-medium.json", tmp_path)
        assert "gofmt" in findings[0].remediation

    def test_non_dict_items_skipped(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "Issues": [
                "not-a-dict",
                {"FromLinter": "gofmt", "Text": "x", "Pos": {"Filename": "a.go", "Line": 1, "Column": 1}},
            ]
        })
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "a.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_golangci_lint(cf, Path("/dev/null"))
        assert len(findings) == 1

    def test_module_root_prefix_prepended(self, tmp_path: Path) -> None:
        """When go.mod is in a subdir, its path prefix is prepended to filenames."""
        subdir = tmp_path / "backend"
        subdir.mkdir()
        go_mod = subdir / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = subdir / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        payload = json.dumps({
            "Issues": [{
                "FromLinter": "gofmt",
                "Text": "not formatted",
                "Pos": {"Filename": "main.go", "Line": 3, "Column": 1},
            }]
        })
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_golangci_lint(cf, Path("/dev/null"))
        assert findings[0].file.endswith("/main.go")


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_golangci_lint_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["main.go"], go=["main.go"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "golangci-lint" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_golangci_lint_skipped_when_no_go_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("golangci-lint", ["go"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
