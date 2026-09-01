"""Tests for compute_diff's handling of a missing/unfetched base ref (issue #702).

On Bitbucket Cloud's single-branch PR clone, `origin/<base_ref>` never exists
locally unless something explicitly fetches it. `git diff` against a missing
ref exits non-zero with empty stdout, which `compute_diff` previously treated
as indistinguishable from a genuine empty diff -- silently skipping the
review with "no changed files" instead of surfacing the real problem.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_pr_review.diff.compute import GitDiffError, compute_diff


@pytest.fixture()
def unfetched_repo(tmp_path: Path) -> dict[str, str]:
    """A repo with a commit but no origin/<base_ref> ref -- simulating a
    single-branch PR clone that never fetched the base branch."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"git {args} failed:\n{r.stderr}"
        return r.stdout.strip()

    git("init", "-q", "-b", "feature")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Tester")
    git("config", "commit.gpgSign", "false")

    (repo / "app.py").write_text("print('hello')\n")
    git("add", "app.py")
    git("commit", "-q", "-m", "feat: add app.py")
    head_sha = git("rev-parse", "HEAD")

    # Deliberately no `origin/main` ref -- this is the bug scenario.
    return {"repo": str(repo), "head_sha": head_sha, "base_ref": "main"}


def test_missing_base_ref_raises_instead_of_silent_empty_diff(
    unfetched_repo: dict[str, str],
) -> None:
    """A missing origin/<base_ref> must fail loudly, not look like "no changes"."""
    with pytest.raises(GitDiffError, match="origin/main"):
        compute_diff(
            base_ref=unfetched_repo["base_ref"],
            head_sha=unfetched_repo["head_sha"],
            workspace=unfetched_repo["repo"],
        )


def test_missing_base_ref_raises_on_stale_watermark_fallback(
    unfetched_repo: dict[str, str],
) -> None:
    """Same failure mode when an unreachable last_reviewed_sha falls back to
    the full origin/<base_ref> range."""
    with pytest.raises(GitDiffError, match="origin/main"):
        compute_diff(
            base_ref=unfetched_repo["base_ref"],
            head_sha=unfetched_repo["head_sha"],
            workspace=unfetched_repo["repo"],
            last_reviewed_sha="0" * 40,
        )
