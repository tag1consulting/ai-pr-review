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

    git("init", "-q", "-b", "main")
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


def test_filtered_changed_files_and_stat_match_filtered_diff(
    merge_repo: dict[str, str],
) -> None:
    """changed_files/diff_stat must describe the same (filtered) range as diff_text.

    Regression guard: a successful filter used to only swap in the filtered
    diff_text, leaving changed_files/diff_stat computed from the pre-filter
    (unfiltered) range — e.g. still listing upstream.txt even though the
    filtered diff_text excluded it. All three must agree.
    """
    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=True,
        review_target="pr",
    )
    assert result.fallback_reason == ""
    assert result.changed_files == ["feature.txt"]
    assert "upstream.txt" not in result.changed_files
    # diff_stat is the summary line only (e.g. "1 file changed, ...") — assert
    # it reflects a single-file change, matching changed_files, not two files
    # (which the unfiltered range, including upstream.txt, would produce).
    assert "1 file changed" in result.diff_stat


def test_filtered_incremental_diff_label_reflects_filtered_range(
    merge_repo: dict[str, str],
) -> None:
    """diff_label must describe the filtered range on a successful incremental filter.

    Regression guard: a successful filter used to leave diff_label as the
    pre-filter "incremental (...)" descriptor even after changed_files/diff_stat
    were switched to the filtered range, making the manifest header (read by the
    model) disagree with the data next to it.
    """
    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=merge_repo["repo"],
        ignore_merge_commits=True,
        review_target="pr",
        last_reviewed_sha=merge_repo["dev_a"],
    )
    assert result.fallback_reason == ""
    assert result.is_incremental
    assert "filtered" in result.diff_label


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


def test_filter_succeeds_with_no_ambient_git_identity(
    merge_repo: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression test for #607.

    Production containers configure no git identity anywhere (no ~/.gitconfig,
    no /etc/gitconfig) — only the *local* repo config, which `merge_repo` sets
    via `git config user.email`/`user.name`. That local config is shared by
    every worktree of the same repo (worktrees share one `.git` config file),
    so `merge_repo`'s existing tests never exercised the container's actual
    environment: no identity at *any* level (local, global, or system).

    `_filtered_diff()`'s cherry-pick loop rebuilds each commit via
    `git commit --no-edit --allow-empty -C <c>` in a scratch worktree. `-C`
    reuses the source commit's author, but git still requires a *committer*
    identity — which is absent here. Without the fix (a synthetic committer
    identity passed to that commit call), this fails on the first commit,
    `_filtered_diff` returns a non-empty fallback_reason, and — before the
    #607 fix — `compute_diff` fell back to the unfiltered incremental diff,
    unbounded by watermark age. The fix must make the filter succeed even
    with zero ambient identity, and must not leave the two-dot fallback in
    place if it ever does fail.
    """
    repo = merge_repo["repo"]

    # Blank local config's identity too (it's the one merge_repo actually
    # set), then remove all HOME/global/system fallbacks.
    subprocess.run(
        ["git", "-C", repo, "config", "--unset", "user.email"], capture_output=True
    )
    subprocess.run(
        ["git", "-C", repo, "config", "--unset", "user.name"], capture_output=True
    )
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)

    result = compute_diff(
        base_ref=merge_repo["base_ref"],
        head_sha=merge_repo["dev_b"],
        workspace=repo,
        ignore_merge_commits=True,
        review_target="pr",
    )

    assert result.fallback_reason == ""
    assert "feature.txt" in result.diff_text
    assert "upstream v2" not in result.diff_text


@pytest.fixture()
def conflicting_merge_repo(tmp_path: Path) -> dict[str, str]:
    """Build a repo where cherry-picking a dev commit onto diff_base conflicts.

    Layout: dev edits shared.txt's line2 on the feature branch; upstream
    edits the same line independently; the developer merges upstream in,
    resolving the conflict by hand; then makes a second dev commit that
    touches shared.txt again post-merge. Using the *upstream* commit as
    last_reviewed_sha (a valid ancestor watermark, reachable via the merge)
    makes compute_diff pick the incremental two-dot range first. Cherry-
    picking the post-merge dev commit onto a worktree rooted at that
    watermark then genuinely conflicts: the worktree has the pre-resolution
    upstream text, but the commit's diff expects the post-resolution text.
    This is what a real "cherry-pick conflict during merge-commit filtering"
    fallback looks like (distinct from the identity-failure fallback above,
    and it must exercise the *incremental* path to test fallback boundedness
    at all — a non-incremental review already uses the bounded three-dot
    range with nothing to fall back from).
    """
    repo = tmp_path / "conflict-repo"
    repo.mkdir()

    def git(*args: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"git {args} failed:\n{r.stderr}"
        return r.stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Tester")
    git("config", "commit.gpgSign", "false")

    (repo / "shared.txt").write_text("line1\nline2\nline3\n")
    git("add", "shared.txt")
    git("commit", "-q", "-m", "base")
    main_sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", main_sha)

    git("checkout", "-q", "-b", "feature")
    (repo / "shared.txt").write_text("line1\nDEV CHANGE\nline3\n")
    git("add", "shared.txt")
    git("commit", "-q", "-m", "feat: dev edits line2")

    git("checkout", "-q", "main")
    (repo / "shared.txt").write_text("line1\nUPSTREAM CHANGE\nline3\n")
    git("add", "shared.txt")
    git("commit", "-q", "-m", "upstream: edits line2 too")
    upstream_v2 = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", upstream_v2)

    git("checkout", "-q", "feature")
    # Merge main into feature, resolving the conflict by hand.
    subprocess.run(["git", "-C", str(repo), "merge", "--no-edit", "main"], capture_output=True)
    (repo / "shared.txt").write_text("line1\nRESOLVED\nline3\n")
    git("add", "shared.txt")
    git("commit", "-q", "-m", "Merge main into feature")

    # Second dev commit after the merge, also touching shared.txt — this is
    # the one that will fail to cherry-pick onto a worktree rooted at
    # upstream_v2 (pre-resolution text).
    (repo / "shared.txt").write_text("line1\nRESOLVED\nline3\nline4 new\n")
    git("add", "shared.txt")
    git("commit", "-q", "-m", "feat: dev commit B touches shared.txt again")
    dev_b = git("rev-parse", "HEAD")

    return {
        "repo": str(repo),
        "base_ref": "main",
        "head_sha": dev_b,
        "watermark": upstream_v2,
    }


def test_bounded_fallback_on_real_cherry_pick_conflict(
    conflicting_merge_repo: dict[str, str],
) -> None:
    """Regression test for #607's fallback-boundedness fix.

    Uses last_reviewed_sha=watermark (a valid ancestor of head, so
    compute_diff picks the *incremental* two-dot range first — the case
    that actually exercises the bug: an unbounded fallback only matters
    when the initial range was incremental, since a non-incremental review
    already uses the bounded three-dot range). When the filter then hits a
    genuine cherry-pick conflict (not the identity failure covered above),
    compute_diff must fall back to the bounded three-dot diff instead of
    keeping the unfiltered incremental one — and every DiffResult field
    (changed_files, diff_stat, diff_text, diff_label, is_incremental) must
    describe that same three-dot range consistently.
    """
    result = compute_diff(
        base_ref=conflicting_merge_repo["base_ref"],
        head_sha=conflicting_merge_repo["head_sha"],
        workspace=conflicting_merge_repo["repo"],
        last_reviewed_sha=conflicting_merge_repo["watermark"],
        ignore_merge_commits=True,
        review_target="pr",
    )

    assert result.fallback_reason == "cherry-pick conflict during merge-commit filtering"
    assert result.is_incremental is False
    assert result.diff_label.startswith("full (")
    assert "shared.txt" in result.changed_files
    assert "RESOLVED" in result.diff_text
    assert "1 file changed" in result.diff_stat
