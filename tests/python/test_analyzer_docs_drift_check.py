"""Tests for the native docs-drift-check analyzer."""

from __future__ import annotations

import subprocess as sp
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.docs_drift import (
    _filter_renamed_paths,
    _parse_deleted_and_added_paths,
    _run_docs_drift_check,
)
from ai_pr_review.manifest import ChangedFiles

_DELETE_DIFF = """\
diff --git a/lib/agents.sh b/lib/agents.sh
deleted file mode 100644
index abc123..0000000
--- a/lib/agents.sh
+++ /dev/null
@@ -1,3 +0,0 @@
-#!/bin/bash
-echo hi
-exit 0
"""

_RENAME_DIFF = """\
diff --git a/lib/agents.sh b/lib/agents.sh
deleted file mode 100644
index abc123..0000000
--- a/lib/agents.sh
+++ /dev/null
diff --git a/tools/agents.sh b/tools/agents.sh
new file mode 100644
index 0000000..def456
--- /dev/null
+++ b/tools/agents.sh
"""

_ADD_DIFF = """\
diff --git a/new/path.py b/new/path.py
new file mode 100644
index 0000000..def456
--- /dev/null
+++ b/new/path.py
@@ -0,0 +1,2 @@
+import os
+print(os.getcwd())
"""


def _make_cf() -> ChangedFiles:
    return ChangedFiles(all_files=[])


def _write_diff(tmp_path: Path, text: str) -> Path:
    diff_file = tmp_path / "diff.txt"
    diff_file.write_text(text)
    return diff_file


class TestDiffParsing:
    """Direct, non-mocked coverage of the regex-based diff parser."""

    def test_deleted_file_detected(self) -> None:
        deleted, added = _parse_deleted_and_added_paths(_DELETE_DIFF)
        assert deleted == {"lib/agents.sh"}
        assert added == set()

    def test_added_file_detected(self) -> None:
        deleted, added = _parse_deleted_and_added_paths(_ADD_DIFF)
        assert added == {"new/path.py"}
        assert deleted == set()

    def test_rename_produces_matching_delete_and_add(self) -> None:
        deleted, added = _parse_deleted_and_added_paths(_RENAME_DIFF)
        assert deleted == {"lib/agents.sh"}
        assert added == {"tools/agents.sh"}

    def test_no_diff_git_headers_returns_empty_sets(self) -> None:
        deleted, added = _parse_deleted_and_added_paths("not a diff at all\njust text\n")
        assert deleted == set()
        assert added == set()

    def test_filter_renamed_paths_drops_basename_match(self) -> None:
        deleted, added = _parse_deleted_and_added_paths(_RENAME_DIFF)
        filtered = _filter_renamed_paths(deleted, added)
        assert filtered == set()

    def test_filter_renamed_paths_keeps_genuine_deletion(self) -> None:
        deleted, added = _parse_deleted_and_added_paths(_DELETE_DIFF)
        filtered = _filter_renamed_paths(deleted, added)
        assert filtered == {"lib/agents.sh"}

    def test_filter_renamed_paths_different_basenames_both_kept(self) -> None:
        deleted = {"lib/agents.sh"}
        added = {"lib/other.sh"}
        assert _filter_renamed_paths(deleted, added) == {"lib/agents.sh"}


class TestRunDocsDriftCheckGuards:
    def test_no_deletions_returns_empty_without_invoking_rg(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _ADD_DIFF)
        cf = _make_cf()
        with patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run:
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []
        mock_run.assert_not_called()

    def test_rename_only_returns_empty_without_invoking_rg(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _RENAME_DIFF)
        cf = _make_cf()
        with patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run:
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []
        mock_run.assert_not_called()

    def test_unreadable_diff_file_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        missing = tmp_path / "does-not-exist.diff"
        cf = _make_cf()
        with caplog.at_level("WARNING"):
            result = _run_docs_drift_check(cf, missing)
        assert result == []
        assert "could not read diff file" in caplog.text

    def test_rg_absent_returns_empty(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value=None):
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []

    def test_rg_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_docs_drift_check(cf, diff_file)
        assert "rg not found" in caplog.text

    def test_rg_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch(
                "ai_pr_review.analyzers.native.docs_drift.subprocess.run",
                side_effect=sp.TimeoutExpired(cmd="rg", timeout=30),
            ),
            caplog.at_level("WARNING"),
        ):
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []
        assert "timed out" in caplog.text

    def test_rg_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch(
                "ai_pr_review.analyzers.native.docs_drift.subprocess.run",
                side_effect=OSError("not found"),
            ),
            caplog.at_level("WARNING"),
        ):
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []

    def test_rg_bad_exit_code_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="bad invocation")
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []
        assert "exited 2" in caplog.text

    def test_rg_exit_1_no_matches_returns_empty(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = _run_docs_drift_check(cf, diff_file)
        assert result == []


class TestRunDocsDriftCheckFindings:
    def test_single_match_produces_finding_with_expected_fields(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="memory-bank/some-doc.md:42:see lib/agents.sh for details\n",
                stderr="",
            )
            findings = _run_docs_drift_check(cf, diff_file)

        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Low"
        assert f.confidence == 80
        assert f.source == "docs-drift-check"
        assert f.category == "docs"
        assert f.file == "memory-bank/some-doc.md"
        assert f.line == 42
        assert "lib/agents.sh" in f.finding
        assert "lib/agents.sh" in f.remediation

    def test_leading_dot_slash_stripped_from_file(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="./README.md:1:see lib/agents.sh\n",
                stderr="",
            )
            findings = _run_docs_drift_check(cf, diff_file)
        assert findings[0].file == "README.md"

    def test_multiple_matches_produce_separate_findings(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "docs/a.md:1:see lib/agents.sh\n"
                    "docs/b.md:2:also see lib/agents.sh\n"
                    "docs/c.md:3:lib/agents.sh again\n"
                ),
                stderr="",
            )
            findings = _run_docs_drift_check(cf, diff_file)

        assert len(findings) == 3
        assert {f.file for f in findings} == {"docs/a.md", "docs/b.md", "docs/c.md"}

    def test_self_reference_skipped(self, tmp_path: Path) -> None:
        # lib/agents.sh is being deleted; rg matching a hit inside a file that
        # is itself being deleted in this same diff should not produce noise.
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="lib/agents.sh:1:# see lib/agents.sh\n",
                stderr="",
            )
            findings = _run_docs_drift_check(cf, diff_file)
        assert findings == []

    def test_rename_filters_out_deletion_no_findings(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _RENAME_DIFF)
        cf = _make_cf()
        with patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run:
            findings = _run_docs_drift_check(cf, diff_file)
        assert findings == []
        mock_run.assert_not_called()

    def test_malformed_rg_line_skipped(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not-a-valid-rg-line-without-colons\n",
                stderr="",
            )
            findings = _run_docs_drift_check(cf, diff_file)
        assert findings == []

    def test_non_numeric_line_number_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="docs/a.md:not-a-number:see lib/agents.sh\n",
                stderr="",
            )
            findings = _run_docs_drift_check(cf, diff_file)
        assert findings == []
        assert "non-numeric line number" in caplog.text

    def test_rg_invoked_with_expected_globs_and_pattern(self, tmp_path: Path) -> None:
        diff_file = _write_diff(tmp_path, _DELETE_DIFF)
        cf = _make_cf()
        with (
            patch("ai_pr_review.analyzers.native.docs_drift.shutil.which", return_value="/usr/bin/rg"),
            patch("ai_pr_review.analyzers.native.docs_drift.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            _run_docs_drift_check(cf, diff_file)

        call_args = mock_run.call_args[0][0]
        assert "rg" in call_args
        assert "--fixed-strings" in call_args
        assert "-g" in call_args
        assert "*.md" in call_args
        assert "*.txt" in call_args
        assert "*.rst" in call_args
        assert "!vendor/*" in call_args
        assert "!node_modules/*" in call_args
        assert "lib/agents.sh" in call_args
