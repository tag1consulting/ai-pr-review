"""Shared path-normalization helper for analyzers backed by tools that
resolve reported paths to absolute (issue #713).

ruff's JSON `filename` field is always an absolute, cwd-resolved path,
regardless of whether the file was named relatively on the command line
(verified directly: `ruff check --output-format=json -- ai_pr_review/x.py`,
run from the repo root, still reports `filename` as
`/abs/path/to/repo/ai_pr_review/x.py`). Every other analyzer/agent in this
review reports `Finding.file` as repo-relative, matching the diff and the
`changed_files` manifest it is built from (see manifest.py, which is itself
unaffected -- it categorizes whatever `git diff --name-only` already
produced, which is repo-relative). Any analyzer wrapping a tool with this
"always absolute" behavior must strip that resolution back off before
building a Finding.

`native/ruff.py`'s own general-purpose "ruff" analyzer already did this.
`native/docs_comments.py`'s separate `ruff --isolated` invocation (a
deliberately different call so it never inherits the consumer's own ruff
config -- see that module's docstring) did not, which is what let an
absolute container path (`/workspace/...`) leak into a docs-api-check
Finding. This module exists so both call sites share one implementation
instead of the same fix drifting out of sync a second time.
"""

from __future__ import annotations

import os


def strip_workspace_prefix(filename: str) -> str:
    """Strip a GITHUB_WORKSPACE- or cwd-based absolute prefix from *filename*.

    Uses `GITHUB_WORKSPACE` when set (the repo checkout root inside the
    GitHub Actions container this action normally runs in) and falls back to
    the process's own current working directory otherwise (local/dev runs,
    where cwd is expected to already be the repo root). A filename that does
    not start with that prefix is returned unchanged.
    """
    workspace_prefix = (os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).rstrip("/") + "/"
    if filename.startswith(workspace_prefix):
        return filename[len(workspace_prefix):]
    return filename
