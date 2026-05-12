"""Diff computation — full and incremental git diff, changed-file list."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class DiffResult:
    diff_text: str
    changed_files: list[str]
    diff_stat: str
    diff_label: str
    base: str
    head: str
    is_incremental: bool


# Paths excluded from diff (lockfiles, vendor, generated).
_EXCLUDE_PATTERNS = [
    ":!*lock.json",
    ":!*lock.yaml",
    ":!*.lock",
    ":!*.sum",
    ":!vendor/*",
    ":!node_modules/*",
]


def compute_diff(
    base_ref: str,
    head_sha: str,
    *,
    last_reviewed_sha: str = "",
    workspace: str = ".",
) -> DiffResult:
    """Compute a git diff between base and head.

    Uses incremental diff (last_reviewed_sha..head_sha) when a valid
    watermark SHA is provided, falling back to the full PR diff.
    """
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

    # Changed files (with exclusions)
    changed_result = subprocess.run(
        git + ["diff", "--name-only", range_spec] + ["--"] + _EXCLUDE_PATTERNS,
        capture_output=True,
        text=True,
    )
    changed_files = [
        f for f in changed_result.stdout.splitlines() if f
    ]

    # Diff stat
    stat_result = subprocess.run(
        git + ["diff", "--stat", range_spec] + ["--"] + _EXCLUDE_PATTERNS,
        capture_output=True,
        text=True,
    )
    diff_stat = stat_result.stdout.strip().splitlines()[-1] if stat_result.stdout.strip() else ""

    # Full diff text
    diff_result = subprocess.run(
        git + ["diff", range_spec] + ["--"] + _EXCLUDE_PATTERNS,
        capture_output=True,
        text=True,
    )
    diff_text = diff_result.stdout

    return DiffResult(
        diff_text=diff_text,
        changed_files=changed_files,
        diff_stat=diff_stat,
        diff_label=label,
        base=base_ref,
        head=head_sha,
        is_incremental=incremental,
    )
