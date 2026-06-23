"""Security canary tests: verify the action never executes checked-out workspace files.

Each test exercises a code path that reads files from $GITHUB_WORKSPACE as data and
asserts that none of the sentinel files in CANARY_DIR were written. A sentinel file
appearing means the code path *executed* a workspace file rather than just reading it.

These tests are marked @pytest.mark.security. A failure is a security regression, not
a flaky test — see SECURITY.md and CONTRIBUTING.md for the reporting and remediation
process.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

CANARY_WORKSPACE = Path(__file__).parent.parent / "security" / "canary-workspace"

SENTINEL_NAMES = [
    "makefile-executed",
    "setup-py-imported",
    "npm-preinstall-executed",
    "npm-postinstall-executed",
    "pre-commit-hook-executed",
    "conftest-imported",
    "semgrep-rule-executed",
]


def _assert_no_canary_fired(canary_dir: Path) -> None:
    """Assert no sentinel file was written under canary_dir."""
    if not canary_dir.exists():
        return
    fired = [f.name for f in canary_dir.iterdir() if f.name in SENTINEL_NAMES]
    assert not fired, (
        f"SECURITY REGRESSION: workspace execution sentinels triggered: {fired}. "
        "The action executed checked-out code from the workspace, violating the "
        "'never execute checked-out code' invariant. "
        "See SECURITY.md and CONTRIBUTING.md for the reporting process."
    )


@pytest.mark.security
class TestNeverExecuteCheckedOutCode:
    """Verify each workspace-reading code path treats files as data, never as code."""

    def test_language_detection_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """languages.py detects language by extension/basename — must not exec any file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.languages import detect_language, is_test_file

        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file():
                detect_language(f.suffix.lstrip("."))
                is_test_file(str(f))

        _assert_no_canary_fired(canary_dir)

    def test_manifest_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """manifest.py classifies files by path/extension — must not exec any file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.manifest import build_changed_files

        file_list = [
            str(f.relative_to(CANARY_WORKSPACE))
            for f in CANARY_WORKSPACE.iterdir()
            if f.is_file()
        ]
        result = build_changed_files(file_list)
        assert result is not None

        _assert_no_canary_fired(canary_dir)

    def test_analyzer_bridge_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """analyzer bridge runs regex/AST analysis on file content — must not exec files."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.analyzers.bridge import run_analyzers
        from ai_pr_review.manifest import build_changed_files

        file_list = [
            str(f.relative_to(CANARY_WORKSPACE))
            for f in CANARY_WORKSPACE.iterdir()
            if f.is_file()
        ]
        changed_files = build_changed_files(file_list)

        # Write a minimal diff file so the analyzer has something to read as data.
        diff_file = str(tmp_path / "test.diff")
        Path(diff_file).write_text("", encoding="utf-8")

        # A missing-dep or I/O error is acceptable; execution of canary files is not.
        with contextlib.suppress(Exception):
            asyncio.run(
                run_analyzers(changed_files=changed_files, diff_file=diff_file)
            )

        _assert_no_canary_fired(canary_dir)

    def test_sarif_ingestor_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SARIF ingestor reads JSON as data — must not execute any workspace file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.analyzers.sarif import load_sarif_files

        # load_sarif_files is fail-soft — a missing path returns [] and logs a warning.
        findings, _elapsed = load_sarif_files(["nonexistent.sarif"])
        assert findings == []

        _assert_no_canary_fired(canary_dir)

    def test_context_enrichment_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tree-sitter parses file content as AST — must not exec any workspace file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))

        try:
            from ai_pr_review.context.treesitter import extract_symbol_refs
        except ImportError:
            pytest.skip("tree-sitter-language-pack not installed")

        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file() and f.suffix == ".py":
                # Pass file content as a diff hunk string — purely data, never executed.
                hunk = f.read_text(errors="replace")
                extract_symbol_refs(hunk, language="python")

        _assert_no_canary_fired(canary_dir)

    def test_language_profile_loading_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """language_profiles.py reads markdown from the image install — not workspace."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.language_profiles import load_language_profiles

        # Point script_dir at a temp dir with no language-profiles/ subdir — the
        # function is fail-soft and returns "" when the directory doesn't exist.
        # This verifies it never falls back to reading from CANARY_WORKSPACE.
        result = load_language_profiles(["Python", "JavaScript"], script_dir=tmp_path)
        assert isinstance(result, str)

        # Also verify with a real script_dir that has profiles (if available).
        # Either way, no workspace files should be executed.
        import ai_pr_review

        real_script_dir = Path(ai_pr_review.__file__).parent
        load_language_profiles(["Python"], script_dir=real_script_dir)

        _assert_no_canary_fired(canary_dir)

    def test_reading_workspace_files_as_bytes_does_not_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading canary files as raw bytes (simulating diff content reads) fires no sentinel."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))

        # Simulate what diff/compute.py does: open and read file bytes for diffing.
        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file():
                _ = f.read_bytes()

        _assert_no_canary_fired(canary_dir)
