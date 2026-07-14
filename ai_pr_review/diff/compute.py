"""Diff computation — full and incremental git diff, changed-file list."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DiffResult:
    diff_text: str
    changed_files: list[str]
    diff_stat: str
    diff_label: str
    base: str
    head: str
    is_incremental: bool
    fallback_reason: str = field(default="")


# Paths excluded from diff (lockfiles, vendor, generated).
_EXCLUDE_PATTERNS = [
    ":!*lock.json",
    ":!*lock.yaml",
    ":!*.lock",
    ":!*.sum",
    ":!vendor/*",
    ":!node_modules/*",
]


def _resolve_excludes(user_patterns: tuple[str, ...], mode: str) -> list[str]:
    """Merge user-supplied exclude patterns with the built-in defaults.

    Args:
        user_patterns: Glob patterns supplied by the caller (without the ``":!"``
            pathspec prefix — the prefix is added automatically if missing).
        mode: ``"append"`` (default) adds *user_patterns* after the built-in list.
            ``"replace"`` uses *only* user_patterns and drops the built-ins.
            ``"replace"`` with an empty *user_patterns* falls back to the built-ins
            with a warning rather than producing an unfiltered diff silently.

    Returns:
        A list of pathspec-prefixed glob patterns ready to pass to ``git diff``.
    """

    def _normalize(p: str) -> str:
        return p if p.startswith(":!") else f":!{p}"

    normalized = [_normalize(p) for p in user_patterns]

    if mode == "replace":
        if not normalized:
            logger.warning(
                "AI_EXCLUDE_PATTERNS_MODE=replace with no patterns supplied; "
                "falling back to built-in excludes to avoid an unfiltered diff."
            )
            return list(_EXCLUDE_PATTERNS)
        return normalized

    # Default: "append" — built-ins first, then user patterns.
    return list(_EXCLUDE_PATTERNS) + normalized


def _diff_variants(
    git: list[str], range_spec: str, excludes: list[str]
) -> tuple[list[str], str, str]:
    """Run the three git-diff variants (names, stat, full text) for range_spec."""
    changed_result = subprocess.run(
        git + ["diff", "--name-only", range_spec] + ["--"] + excludes,
        capture_output=True,
        text=True,
    )
    changed_files = [f for f in changed_result.stdout.splitlines() if f]

    stat_result = subprocess.run(
        git + ["diff", "--stat", range_spec] + ["--"] + excludes,
        capture_output=True,
        text=True,
    )
    diff_stat = stat_result.stdout.strip().splitlines()[-1] if stat_result.stdout.strip() else ""

    diff_result = subprocess.run(
        git + ["diff", range_spec] + ["--"] + excludes,
        capture_output=True,
        text=True,
    )
    return changed_files, diff_stat, diff_result.stdout


def _filtered_diff(
    diff_base: str,
    head_sha: str,
    base_ref: str,
    workspace: str = ".",
    exclude_patterns: list[str] | None = None,
) -> tuple[list[str] | None, str, str, str]:
    """Filter base-branch merge commits out of diff_base..head_sha.

    Identifies merge commits in diff_base..head_sha whose second parent is
    reachable from origin/<base_ref> (i.e. "merge main into feature" commits).
    Cherry-picks all non-merge commits from the range onto a synthetic branch
    and diffs that against diff_base.

    Args:
        exclude_patterns: Resolved pathspec patterns to pass to ``git diff``.
            Defaults to ``_EXCLUDE_PATTERNS`` when *None*.

    Returns (changed_files, diff_stat, diff_text, fallback_reason). On success
    or no-op, fallback_reason="" and changed_files/diff_stat/diff_text describe
    the filtered range (changed_files is None on no-op — caller keeps its own).
    On conflict or git error, returns (None, "", "", "<reason>") — the caller
    falls back to a full diff.
    """
    if exclude_patterns is None:
        exclude_patterns = list(_EXCLUDE_PATTERNS)
    git = ["git", "-C", workspace]

    # 1. List merge commits in range
    merges_result = subprocess.run(
        git + ["rev-list", "--merges", f"{diff_base}..{head_sha}"],
        capture_output=True,
        text=True,
    )
    merges = [m for m in merges_result.stdout.splitlines() if m]
    if not merges:
        return (None, "", "", "")  # no merges — signal no-op

    # 2. Filter to those whose M^2 is reachable from origin/<base_ref>
    origin_base = f"origin/{base_ref}"
    qualifying: list[str] = []
    for m in merges:
        p2_result = subprocess.run(
            git + ["rev-parse", f"{m}^2"],
            capture_output=True,
            text=True,
        )
        if p2_result.returncode != 0:
            continue
        p2 = p2_result.stdout.strip()
        is_ancestor = subprocess.run(
            git + ["merge-base", "--is-ancestor", p2, origin_base],
            capture_output=True,
        ).returncode == 0
        if is_ancestor:
            qualifying.append(m)

    if not qualifying:
        return (None, "", "", "")  # no base-branch merges to filter — signal no-op

    logger.info(
        "Merge-commit filter: %d upstream merge(s) found; building synthetic branch.",
        len(qualifying),
    )

    # 3. Cherry-pick non-merge commits into a temp worktree
    # Exclude commits reachable from origin/<base_ref> so upstream commits
    # pulled in by the merge are not cherry-picked.
    non_merges_result = subprocess.run(
        git + [
            "rev-list", "--reverse", "--no-merges",
            f"{diff_base}..{head_sha}",
            "--not", f"origin/{base_ref}",
        ],
        capture_output=True,
        text=True,
    )
    commits = [c for c in non_merges_result.stdout.splitlines() if c]

    with tempfile.TemporaryDirectory(prefix="ai-review-filter-") as tmpdir:
        # Add a temporary worktree rooted at diff_base
        add_wt = subprocess.run(
            git + ["worktree", "add", "--quiet", "--detach", tmpdir, diff_base],
            capture_output=True,
        )
        if add_wt.returncode != 0:
            reason = "could not create git worktree for merge-commit filtering"
            logger.warning("Merge-commit filter: %s", reason)
            return (None, "", "", reason)

        try:
            wt_git = ["git", "-C", tmpdir]
            for c in commits:
                pick = subprocess.run(
                    wt_git + ["cherry-pick", "--no-commit", c],
                    capture_output=True,
                )
                if pick.returncode != 0:
                    subprocess.run(
                        wt_git + ["cherry-pick", "--abort"],
                        capture_output=True,
                    )
                    reason = "cherry-pick conflict during merge-commit filtering"
                    logger.warning("Merge-commit filter: %s", reason)
                    return (None, "", "", reason)
                commit_r = subprocess.run(
                    wt_git + [
                        "-c", "user.name=ai-pr-review",
                        "-c", "user.email=ai-pr-review@localhost",
                        "commit", "--no-edit", "--allow-empty", "-C", c,
                    ],
                    capture_output=True,
                )
                if commit_r.returncode != 0:
                    reason = "cherry-pick commit failed during merge-commit filtering"
                    logger.warning("Merge-commit filter: %s", reason)
                    return (None, "", "", reason)

            # 4. Diff diff_base..synthetic_tip — compute changed_files/diff_stat
            # here too (not just diff_text), and do it before the `finally`
            # below removes the worktree: synthetic_tip only exists in it.
            tip_result = subprocess.run(
                wt_git + ["rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            synthetic_tip = tip_result.stdout.strip()
            if not synthetic_tip or synthetic_tip == diff_base:
                # No commits cherry-picked — return empty diff
                return (None, "", "", "")

            filtered_range = f"{diff_base}..{synthetic_tip}"
            changed_files, diff_stat, filtered_text = _diff_variants(
                git, filtered_range, exclude_patterns
            )
            logger.info(
                "Merge-commit filter: synthetic diff ready (%s..%s).",
                diff_base[:7],
                synthetic_tip[:7],
            )
            return (changed_files, diff_stat, filtered_text, "")
        finally:
            subprocess.run(
                git + ["worktree", "remove", "--force", tmpdir],
                capture_output=True,
            )


def compute_diff(
    base_ref: str,
    head_sha: str,
    *,
    last_reviewed_sha: str = "",
    workspace: str = ".",
    ignore_merge_commits: bool = True,
    review_target: str = "pr",
    exclude_patterns: tuple[str, ...] = (),
    exclude_patterns_mode: str = "append",
) -> DiffResult:
    """Compute a git diff between base and head.

    Uses incremental diff (last_reviewed_sha..head_sha) when a valid
    watermark SHA is provided, falling back to the full PR diff.

    When ignore_merge_commits=True (and review_target != "standalone"), strips
    merge commits that pulled in upstream base-branch changes and diffs only
    the PR author's own commits.  Falls back to the unfiltered diff on conflict.

    Args:
        exclude_patterns: User-supplied git pathspec glob patterns to exclude from
            the diff (e.g. ``("docs/*", "*.generated.go")``). The ``":!"`` prefix
            is added automatically. Interacts with *exclude_patterns_mode*.
        exclude_patterns_mode: ``"append"`` (default) adds *exclude_patterns* to
            the built-in lockfile/vendor excludes. ``"replace"`` uses only
            *exclude_patterns* and drops the built-in list. Providing ``"replace"``
            with an empty *exclude_patterns* logs a warning and falls back to the
            built-ins.
    """
    excludes = _resolve_excludes(exclude_patterns, exclude_patterns_mode)
    git = ["git", "-C", workspace]

    # Determine diff base
    incremental = False
    if last_reviewed_sha:
        reachable = (
            subprocess.run(
                git + ["cat-file", "-e", f"{last_reviewed_sha}^{{commit}}"],
                capture_output=True,
            ).returncode
            == 0
        )
        is_ancestor = (
            subprocess.run(
                git + ["merge-base", "--is-ancestor", last_reviewed_sha, head_sha],
                capture_output=True,
            ).returncode
            == 0
        )
        if reachable and is_ancestor:
            diff_base = last_reviewed_sha
            incremental = True
        else:
            diff_base = f"origin/{base_ref}"
    else:
        diff_base = f"origin/{base_ref}"

    if incremental:
        range_spec = f"{diff_base}..{head_sha}"
        label = f"incremental ({diff_base[:7]}..{head_sha[:7]})"
    else:
        range_spec = f"{diff_base}...{head_sha}"
        label = f"full ({base_ref}..{head_sha[:7]})"

    changed_files, diff_stat, diff_text = _diff_variants(git, range_spec, excludes)
    fallback_reason = ""

    # Optionally filter out upstream base-branch merges
    if ignore_merge_commits and review_target != "standalone":
        filtered_files, filtered_stat, filtered_text, fallback_reason = _filtered_diff(
            diff_base, head_sha, base_ref, workspace, exclude_patterns=excludes
        )
        if fallback_reason:
            # Filter failed (identity error, real conflict, worktree error, ...).
            # Falling back to the *unfiltered incremental* range is unsafe: it is
            # unbounded by watermark age and can balloon to thousands of lines
            # when last_reviewed_sha is stale (see issue #607). Recompute the
            # full three-dot range instead, which is bounded by the merge-base
            # regardless of how old the watermark is, and re-derive every field
            # of the result from it so changed_files/diff_stat/diff_text/label
            # all describe the same range.
            logger.warning(
                "Merge-commit filter failed (%s); falling back to full diff "
                "(unfiltered incremental range is unbounded).",
                fallback_reason,
            )
            if incremental:
                range_spec = f"origin/{base_ref}...{head_sha}"
                label = f"full ({base_ref}..{head_sha[:7]})"
                incremental = False
                changed_files, diff_stat, diff_text = _diff_variants(
                    git, range_spec, excludes
                )
            # else: already computed the full three-dot range above — no-op.
        elif filtered_files is not None:
            # Successfully filtered — use the filtered range's changed_files
            # and diff_stat too, not just diff_text, so all three describe
            # the same (filtered) range instead of the pre-filter one.
            changed_files = filtered_files
            diff_stat = filtered_stat
            diff_text = filtered_text
        # filtered_files is None and fallback_reason == "" means no qualifying merges — no-op

    return DiffResult(
        diff_text=diff_text,
        changed_files=changed_files,
        diff_stat=diff_stat,
        diff_label=label,
        base=base_ref,
        head=head_sha,
        is_incremental=incremental,
        fallback_reason=fallback_reason,
    )
