"""Tests for compute_diff ignore_merge_commits feature."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_pr_review.diff.compute import compute_diff


@pytest.fixture()
def merge_repo(tmp_path: Path) -> dict[str, str]:
    """Build a git repo with an upstream merge commit scenario.

    Layout:
      main@v1 (MAIN_SHA) — origin/main initially
        └─ feature branch:
             dev commit A (DEV_A)
             ← upstream adds v2 to main →
             merge main@v2 into feature (MERGE)
             dev commit B (DEV_B)
      main@v2 (UPSTREAM_V2) — origin/main after upstream push
    """
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

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Tester")
    git("config", "commit.gpgSign", "false")

    # Upstream v1
    (repo / "upstream.txt").write_text("upstream v1\n")
    git("add", "upstream.txt")
    git("commit", "-q", "-m", "upstream: v1")
    main_sha = git("rev-parse", "HEAD")

    # origin/main initially points to main_sha
    git("update-ref", "refs/remotes/origin/main", main_sha)

    # Feature branch: dev commit A
    git("checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("dev A\n")
    git("add", "feature.txt")
    git("commit", "-q", "-m", "feat: dev commit A")
    dev_a = git("rev-parse", "HEAD")

    # Upstream adds v2 on main
    git("checkout", "-q", "main")
    (repo / "upstream.txt").write_text("upstream v1\nupstream v2\n")
    git("add", "upstream.txt")
    git("commit", "-q", "-m", "upstream: v2")
    upstream_v2 = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", upstream_v2)

    # Developer merges main into feature
    git("checkout", "-q", "feature")
    git("merge", "-q", "--no-edit", "main")
    merge_sha = git("rev-parse", "HEAD")

    # Dev commit B after the merge
    (repo / "feature.txt").write_text("dev A\ndev B\n")
    git("add", "feature.txt")
    git("commit", "-q", "-m", "feat: dev commit B")
    dev_b = git("rev-parse", "HEAD")

    return {
        "repo": str(repo),
        "main_sha": main_sha,
        "dev_a": dev_a,
        "upstream_v2": upstream_v2,
        "merge_sha": merge_sha,
        "dev_b": dev_b,
        "base_ref": "main",
        "head_sha": dev_b,
    }


def test_no_merges_in_range_is_noop(merge_repo: dict[str, str]) -> None:
    """With no merge commits in the range, ignore_merge_commits is a no-op."""
    result_filtered = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_a"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=True,
        review_target="pr",
    )
    result_normal = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_a"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=False,
        review_target="pr",
    )
    assert result_filtered.diff_text == result_normal.diff_text
    assert result_filtered.fallback_reason == ""


def test_filtered_diff_excludes_upstream_changes(merge_repo: dict[str, str]) -> None:
    """Filtered diff contains dev changes but not upstream-only changes."""
    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=True,
        review_target="pr",
    )
    assert result.fallback_reason == ""
    assert "feature.txt" in result.diff_text
    assert "upstream v2" not in result.diff_text


def test_filtered_diff_contains_dev_changes(merge_repo: dict[str, str]) -> None:
    """Filtered diff still contains the developer's actual work."""
    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=True,
        review_target="pr",
    )
    assert result.fallback_reason == ""
    # Both dev commits' changes should appear
    assert "+dev A" in result.diff_text or "dev A" in result.diff_text


def test_standalone_mode_skips_filtering(merge_repo: dict[str, str]) -> None:
    """In standalone mode, filtering is never applied regardless of flag."""
    result_filtered = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=True,
        review_target="standalone",
    )
    result_normal = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=False,
        review_target="standalone",
    )
    # Standalone uses three-dot diff against origin/base, so both should be identical
    assert result_filtered.diff_text == result_normal.diff_text
    assert result_filtered.fallback_reason == ""


def test_intra_pr_merge_preserved(merge_repo: dict[str, str], tmp_path: Path) -> None:
    """Merge of a sibling feature branch (not from origin/main) is preserved."""
    repo = Path(merge_repo["repo"])

    def git(*args: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"git {args} failed:\n{r.stderr}"
        return r.stdout.strip()

    # Create a sibling branch from MAIN_SHA (not from origin/main's latest)
    git("checkout", "-q", "-b", "sibling", merge_repo["main_sha"])
    (repo / "sibling.txt").write_text("sibling work\n")
    git("add", "sibling.txt")
    git("commit", "-q", "-m", "sibling: work")

    # Merge sibling into feature
    git("checkout", "-q", "feature")
    git("merge", "-q", "--no-edit", "sibling")
    intra_merge = git("rev-parse", "HEAD")

    # diff_base = merge_sha (after the upstream merge); the only new thing
    # is the intra-PR merge of sibling
    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=intra_merge,
        workspace=merge_repo["repo"],
        last_reviewed_sha=merge_repo["merge_sha"],
        ignore_merge_commits=True,
        review_target="pr",
    )
    # The intra-PR merge is not from origin/main — no filtering, diff is normal
    assert result.fallback_reason == ""
    # sibling.txt change should be in the diff (not filtered out)
    assert "sibling.txt" in result.diff_text


def test_fallback_reason_populated_on_default_flag(merge_repo: dict[str, str]) -> None:
    """When ignore_merge_commits=False, fallback_reason is always empty."""
    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=False,
        review_target="pr",
    )
    assert result.fallback_reason == ""
