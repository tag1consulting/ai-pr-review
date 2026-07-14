"""Compute phase: diff computation, manifest building, handoff payload."""

from __future__ import annotations

from ai_pr_review.config import ReviewConfig


def run_compute(
    config: ReviewConfig,
    last_reviewed_sha: str | None = None,
) -> dict[str, object]:
    """Execute compute phase and return the handoff payload dict.

    Returns a dict matching the handoff JSON schema (docs/compute-output-schema.md).
    """
    from ai_pr_review.diff.compute import compute_diff
    from ai_pr_review.manifest import build_changed_files, build_manifest_text

    diff_result = compute_diff(
        base_ref=config.base_ref,
        head_sha=config.head_sha,
        workspace=".",
        ignore_merge_commits=config.ignore_merge_commits,
        review_target=config.review_target,
        last_reviewed_sha="" if config.force_full_diff else (last_reviewed_sha or ""),
        exclude_patterns=config.exclude_patterns,
        exclude_patterns_mode=config.exclude_patterns_mode,
    )

    if not diff_result.changed_files:
        return {
            "skip": True,
            "reason": "no changed files",
            "diff": "",
            "changed_files": [],
            "manifest": "",
            "findings": [],
            "token_log": [],
        }

    diff_lines = len(diff_result.diff_text.splitlines())
    if config.max_diff_lines > 0 and diff_lines > config.max_diff_lines:
        reason = f"diff too large ({diff_lines} lines > {config.max_diff_lines})"
        if diff_result.fallback_reason:
            # The merge-commit filter failed and this diff is the fallback
            # range, not the intended filtered one — surface why, so a skip
            # doesn't look inexplicable (see issue #607).
            reason += f"; merge-commit filter failed: {diff_result.fallback_reason}"
        return {
            "skip": True,
            "reason": reason,
            "diff": "",
            "changed_files": diff_result.changed_files,
            "manifest": "",
            "findings": [],
            "token_log": [],
        }

    changed = build_changed_files(diff_result.changed_files)
    manifest_text = build_manifest_text(
        changed,
        base_ref=diff_result.base,
        diff_label=diff_result.diff_label,
        diff_stat=diff_result.diff_stat,
    )

    return {
        "skip": False,
        "reason": "",
        "diff": diff_result.diff_text,
        "changed_files": diff_result.changed_files,
        "manifest": manifest_text,
        "diff_label": diff_result.diff_label,
        "base": diff_result.base,
        "head": diff_result.head,
        "is_incremental": diff_result.is_incremental,
        "languages": changed.languages,
        "merge_filter_fallback_reason": diff_result.fallback_reason,
        "findings": [],
        "token_log": [],
    }
