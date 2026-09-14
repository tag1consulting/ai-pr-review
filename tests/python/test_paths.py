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
