"""Tests for the shared _paths workspace-prefix helper (issue #713).

Both native/ruff.py and native/docs_comments.py wrap tools (ruff) whose JSON
output always reports absolute, cwd-resolved paths regardless of how the file
was named on the command line. This module's `strip_workspace_prefix` is the
one shared implementation both call sites use to bring that back to the
repo-relative form every other analyzer/agent reports in Finding.file.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from ai_pr_review.analyzers.native._paths import strip_workspace_prefix


class TestStripWorkspacePrefix:
    def test_strips_github_workspace_prefix(self) -> None:
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/workspace/ai-pr-review"}):
            result = strip_workspace_prefix("/workspace/ai-pr-review/ai_pr_review/vcs/github.py")
        assert result == "ai_pr_review/vcs/github.py"

    def test_falls_back_to_cwd_when_no_github_workspace(self, tmp_path: object) -> None:
        # Simulate "running from a different cwd than the repo root" (the
        # containerized case) without GITHUB_WORKSPACE set: cwd itself is the
        # resolution base ruff would have used.
        cwd = os.getcwd()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_WORKSPACE", None)
            result = strip_workspace_prefix(f"{cwd}/ai_pr_review/vcs/github.py")
        assert result == "ai_pr_review/vcs/github.py"

    def test_unprefixed_filename_returned_unchanged(self) -> None:
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/workspace/ai-pr-review"}):
            result = strip_workspace_prefix("ai_pr_review/vcs/github.py")
        assert result == "ai_pr_review/vcs/github.py"

    def test_empty_filename_returned_unchanged(self) -> None:
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/workspace/ai-pr-review"}):
            result = strip_workspace_prefix("")
        assert result == ""

    def test_prefix_mismatch_returned_unchanged(self) -> None:
        # A filename absolute under a *different* root than GITHUB_WORKSPACE
        # (e.g. a symlinked or unrelated path) is left alone rather than
        # mangled -- fail-soft, matching this analyzer family's conventions.
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/workspace/ai-pr-review"}):
            result = strip_workspace_prefix("/other/root/ai_pr_review/vcs/github.py")
        assert result == "/other/root/ai_pr_review/vcs/github.py"


class TestStripWorkspacePrefixDiffScopeOutcome:
    """Issue #846: string-shape assertions ("the result equals this repo-
    relative string") are necessary but not sufficient -- #713's actual
    user-visible symptom was an absolute path silently losing diff-scope and
    inline-comment eligibility, since an unstripped path can never match the
    diff's repo-relative (file, line) pairs. These tests exercise the real
    downstream consumers (`findings.scope.apply_diff_scope`,
    `vcs._inline.is_inline_eligible`) with the stripped output, not just the
    stripping function in isolation."""

    def test_stripped_path_stays_in_diff_and_is_inline_eligible(self) -> None:
        from ai_pr_review.diff.linemap import parse_added_lines
        from ai_pr_review.findings.models import Finding
        from ai_pr_review.findings.scope import apply_diff_scope
        from ai_pr_review.vcs._inline import is_inline_eligible

        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/workspace/ai-pr-review"}):
            stripped = strip_workspace_prefix("/workspace/ai-pr-review/ai_pr_review/vcs/github.py")
        assert stripped == "ai_pr_review/vcs/github.py"

        finding = Finding(
            severity="High", confidence=90, finding="test finding", source="phpcs",
            file=stripped, line=42,
        )
        diff_text = (
            "diff --git a/ai_pr_review/vcs/github.py b/ai_pr_review/vcs/github.py\n"
            "index 0000000..1111111 100644\n"
            "--- a/ai_pr_review/vcs/github.py\n"
            "+++ b/ai_pr_review/vcs/github.py\n"
            "@@ -42,1 +42,1 @@\n"
            "-old line\n"
            "+new line\n"
        )

        scoped = apply_diff_scope([finding], diff_text)
        assert len(scoped) == 1
        assert not scoped[0].out_of_diff, "an unstripped path would be silently capped to Low here (#713)"
        assert scoped[0].severity == "High"

        eligible_new = {(lr.file, lr.line) for lr in parse_added_lines(diff_text)}
        assert is_inline_eligible(finding, eligible_new)

    def test_unstripped_absolute_path_would_be_lost_to_out_of_diff(self) -> None:
        """Negative control: without stripping, the exact same finding on the
        exact same diff line is wrongly treated as out-of-diff -- this is the
        #713 bug reproduced directly, to make the positive case above
        meaningful rather than trivially true."""
        from ai_pr_review.findings.models import Finding
        from ai_pr_review.findings.scope import apply_diff_scope

        unstripped_finding = Finding(
            severity="High", confidence=90, finding="test finding", source="phpcs",
            file="/workspace/ai-pr-review/ai_pr_review/vcs/github.py", line=42,
        )
        diff_text = (
            "diff --git a/ai_pr_review/vcs/github.py b/ai_pr_review/vcs/github.py\n"
            "index 0000000..1111111 100644\n"
            "--- a/ai_pr_review/vcs/github.py\n"
            "+++ b/ai_pr_review/vcs/github.py\n"
            "@@ -42,1 +42,1 @@\n"
            "-old line\n"
            "+new line\n"
        )
        scoped = apply_diff_scope([unstripped_finding], diff_text)
        assert scoped[0].out_of_diff
        assert scoped[0].severity == "Low"
