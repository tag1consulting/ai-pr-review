"""CLI entry point for the AI PR Review Python engine.

`python -m ai_pr_review compute` runs the compute phase and writes
a JSON payload to AI_PR_REVIEW_COMPUTE_OUTPUT (S9 handoff).
The bash post-review scripts consume that file.

This is an Epic 1 shim — Epic 2 (#196) will route posting through Python too.
"""

from __future__ import annotations

import json
import sys

import click

from ai_pr_review.config import ConfigError, ReviewConfig


@click.group()
def cli() -> None:
    """AI PR Review — Python compute engine."""


@cli.command()
@click.option(
    "--output",
    envvar="AI_PR_REVIEW_COMPUTE_OUTPUT",
    default="",
    help="Path to write compute output JSON (defaults to AI_PR_REVIEW_COMPUTE_OUTPUT env var).",
)
def compute(output: str) -> None:
    """Run the compute phase (diff, manifest, findings) and write handoff JSON.

    Posting remains in bash for Epic 1; Epic 2 (#196) removes this shim.
    """
    try:
        config = ReviewConfig.from_env()
    except ConfigError as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    payload = _run_compute(config)

    if not output:
        # No output file configured — print to stdout for debugging.
        click.echo(json.dumps(payload, indent=2))
        return

    try:
        with open(output, "w") as fh:
            json.dump(payload, fh, indent=2)
        click.echo(f"Compute output written to {output}", err=True)
    except OSError as exc:
        click.echo(f"ERROR: could not write compute output: {exc}", err=True)
        sys.exit(1)


def _run_compute(config: ReviewConfig) -> dict[str, object]:
    """Execute compute phase and return the handoff payload dict.

    Epic 1 scope: diff computation, language detection, manifest building,
    findings extraction, deduplication, suppression, analyzer bridge,
    and pricing. Agent LLM calls remain in bash.

    Returns a dict matching the handoff JSON schema (docs/compute-output-schema.md).
    """
    from ai_pr_review.diff.compute import compute_diff
    from ai_pr_review.manifest import build_changed_files, build_manifest_text

    # Compute diff
    diff_result = compute_diff(
        base_ref=config.base_ref,
        head_sha=config.head_sha,
        workspace=".",
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

    # Check diff size limit
    diff_lines = len(diff_result.diff_text.splitlines())
    if config.max_diff_lines > 0 and diff_lines > config.max_diff_lines:
        return {
            "skip": True,
            "reason": f"diff too large ({diff_lines} lines > {config.max_diff_lines})",
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
        "findings": [],
        "token_log": [],
    }
