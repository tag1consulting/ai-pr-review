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

    Tries `GITHUB_WORKSPACE` first when set (the repo checkout root inside
    the GitHub Actions container this action normally runs in), then falls
    back to the process's own current working directory (local/dev runs, or
    a self-hosted-runner/`actions/checkout`-with-`path:` layout where
    `GITHUB_WORKSPACE` and the analyzer subprocess's actual cwd diverge). A
    filename that starts with neither candidate prefix is returned
    unchanged.

    This is a genuine try-then-fallback chain (#846 review), not an
    either/or preference: an earlier version used `GITHUB_WORKSPACE` when
    set with no cwd fallback on a prefix mismatch, which silently stopped
    stripping whenever the two diverged -- exactly the class of bug this
    module exists to prevent, and the one CI itself caught (a `chdir`'d test
    failing under a `GITHUB_WORKSPACE` unrelated to the new cwd).
    """
    candidates = []
    github_workspace = os.environ.get("GITHUB_WORKSPACE")
    if github_workspace:
        candidates.append(github_workspace)
    candidates.append(os.getcwd())
    for candidate in candidates:
        workspace_prefix = candidate.rstrip("/") + "/"
        if filename.startswith(workspace_prefix):
            return filename[len(workspace_prefix):]
    return filename
