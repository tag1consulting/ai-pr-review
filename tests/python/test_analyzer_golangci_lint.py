"""Tests for the native golangci-lint analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.golangci_lint import _run_golangci_lint
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "golangci"

# golangci-lint v2 removed the combined "--out-format=json" flag; JSON now
# goes to an explicit --output.json.path (verified against the pinned 2.13.1
# binary this session — the v1 flag errors with "unknown flag"). The code
# under test writes to a real temp file and reads it back rather than
# parsing stdout, so tests simulate the subprocess by writing fixture
# content to whatever path the code passed via --output.json.path=<path>.


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(go_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=go_files, go=go_files)


def _json_path_writer(content: str | None, returncode: int = 0, stderr: str = "") -> Any:
    """Build a subprocess.run side_effect that writes *content* to the
    --output.json.path passed in argv, then returns a matching MagicMock.

    content=None simulates a run that produces no output file at all (e.g.
    a compile error before any linter ran).
    """

    def _run(args: list[str], **kwargs: object) -> MagicMock:
        if content is not None:
            flag = next(a for a in args if a.startswith("--output.json.path="))
            json_path = Path(flag.removeprefix("--output.json.path="))
            json_path.write_text(content)
        return MagicMock(returncode=returncode, stdout="", stderr=stderr)

    return _run


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

    def test_invocation_uses_output_json_path_not_out_format(self, tmp_path: Path) -> None:
        # golangci-lint v2 removed --out-format entirely (v1-only flag);
        # JSON output is requested via --output.json.path=<path> instead.
        # Regression guard for the exact flag this repo's pinned 2.13.1
        # binary requires.
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module example.com/test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf([str(go_file)])
        with (
            patch("ai_pr_review.analyzers.native.golangci_lint.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch(
                "ai_pr_review.analyzers.native.golangci_lint.subprocess.run",
                side_effect=_json_path_writer('{"Issues": []}'),
            ) as mock_run,
        ):
            _run_golangci_lint(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert any(a.startswith("--output.json.path=") for a in call_args)
        assert not any(a.startswith("--out-format") for a in call_args)
        assert "--issues-exit-code=0" in call_args


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
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run", side_effect=_json_path_writer(fixture)),
        ):
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
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run", side_effect=_json_path_writer(fixture)),
            caplog.at_level("WARNING"),
        ):
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
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run", side_effect=_json_path_writer(fixture)),
            caplog.at_level("WARNING"),
        ):
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
            patch(
                "ai_pr_review.analyzers.native.golangci_lint.subprocess.run",
                side_effect=_json_path_writer(None, stderr="compilation error"),
            ),
            caplog.at_level("WARNING"),
        ):
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
                patch(
                    "ai_pr_review.analyzers.native.golangci_lint.subprocess.run",
                    side_effect=_json_path_writer(payload),
                ),
            ):
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
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run", side_effect=_json_path_writer(payload)),
        ):
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
            patch("ai_pr_review.analyzers.native.golangci_lint.subprocess.run", side_effect=_json_path_writer(payload)),
        ):
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
