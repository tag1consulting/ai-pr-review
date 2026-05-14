"""CLI entry point for the AI PR Review Python engine.

Subcommands:
- `compute`: run the compute phase only, writing a JSON payload to
  AI_PR_REVIEW_COMPUTE_OUTPUT. Retained for backwards compatibility with
  the Epic 1 handoff path.
- `review`: end-to-end Python pipeline (compute -> dispatch agents ->
  extract findings -> outcome -> post via VcsProvider). Replaces the
  bash post-review scripts when AI_PR_REVIEW_ENGINE=python.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import click

from ai_pr_review.config import ConfigError, ReviewConfig
from ai_pr_review.manifest import ChangedFiles
from ai_pr_review.orchestrate import ReviewResult


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


@cli.command()
def review() -> None:
    """Run the end-to-end Python review pipeline.

    Builds the VCS provider, runs compute -> dispatch -> post in one process.
    Reads configuration from the same environment variables review.sh
    consumed (PR_NUMBER, BASE_REF, HEAD_SHA, GITHUB_REPOSITORY, etc.) plus
    VCS_PROVIDER for selecting the posting target.

    Exit codes:
      0 — review posted successfully (or skipped cleanly)
      1 — configuration / posting error
    """
    try:
        config = ReviewConfig.from_env()
    except ConfigError as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    try:
        exit_code = anyio.run(_run_review_async, config)
    except Exception as exc:  # noqa: BLE001 — top-level catch for clean exit
        click.echo(f"ERROR: {exc!r}", err=True)
        sys.exit(1)
    sys.exit(exit_code)


async def _run_review_async(config: ReviewConfig) -> int:
    """Execute the review pipeline; return CLI exit code."""
    from ai_pr_review.agents.dispatch import DispatchContext
    from ai_pr_review.agents.gates import evaluate_gates, filter_agents
    from ai_pr_review.agents.roster import AGENTS
    from ai_pr_review.llm.base import LLMRequest, LLMResponse
    from ai_pr_review.llm.client import call_llm
    from ai_pr_review.orchestrate import OrchestrationConfig, run_review
    from ai_pr_review.vcs import provider_from_env
    from ai_pr_review.vcs.protocol import DiffContext

    # 1. Build provider from VCS_PROVIDER env
    provider = provider_from_env()

    # 2. Run compute phase to get diff + manifest
    payload = _run_compute(config)
    if payload.get("skip"):
        reason = str(payload.get("reason") or "no changes")
        click.echo(f"Skipping review: {reason}", err=True)
        result = await _orchestrate_skip(provider, reason)
        return 0 if result.ok else 1

    diff_text = str(payload.get("diff") or "")
    head_sha = str(payload.get("head") or config.head_sha)
    base_ref = str(payload.get("base") or config.base_ref)

    # 3. Build dispatch context
    script_dir = Path(__file__).resolve().parent.parent
    diff_path = Path(os.environ.get("AI_PR_REVIEW_DIFF_FILE") or "/tmp/ai-review-diff.txt")
    diff_path.write_text(diff_text)

    dispatch_ctx = DispatchContext(
        script_dir=script_dir,
        mode=config.review_mode,
        diff_path=diff_path,
        provider=config.provider,
        standard_model=config.model_standard,
        premium_model=config.model_premium,
        enable_suggestions=config.enable_suggestions,
        cache_priming_env=os.environ.get("AI_CACHE_PRIMING") or "false",
        prompt_caching_env=os.environ.get("LLM_PROMPT_CACHING") or "auto",
    )

    # 4. Apply conditional gates
    last_reviewed = provider.get_last_reviewed_sha()
    changed_paths = payload.get("changed_files") or []
    if not isinstance(changed_paths, list):
        changed_paths = []
    cf = _make_changed_files(changed_paths)
    gates = evaluate_gates(
        diff_text=diff_text,
        changed_files=cf,
        env=os.environ,
        last_reviewed_sha=last_reviewed,
    )
    # Filter by mode (full vs quick) first, then by fired gates
    mode_filtered = [
        a for a in AGENTS if not a.full_mode_only or config.review_mode == "full"
    ]
    # pr-summarizer is dispatched via the summarizer module separately;
    # exclude it from the generic dispatch path (run_tier raises otherwise).
    mode_filtered = [a for a in mode_filtered if a.name != "pr-summarizer"]
    agents = filter_agents(mode_filtered, gates)

    # 5. Bind LLM call to the configured provider
    async def _llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, config.provider)

    # 6. Build summary text. For now, leave as-is — the pr-summarizer agent
    # is dispatched separately via summarizer module; this CLI doesn't yet
    # invoke it. Defer summarizer integration to a follow-up commit.
    summary_text = ""

    # 7. Run the orchestrator
    orch_config = OrchestrationConfig(
        mode=config.review_mode,  # type: ignore[arg-type]
        confidence_threshold=config.confidence_threshold,
        max_inline=config.max_inline,
        enable_suggestions=config.enable_suggestions,
        semaphore_size=config.parallel,
    )
    result = await run_review(
        diff=DiffContext(diff_text=diff_text, head_sha=head_sha),
    if not isinstance(provider, VcsProvider):
        raise TypeError(f"Expected VcsProvider, got {type(provider).__name__}")
        agents=agents,
        llm_call=_llm_call,
        dispatch_context=dispatch_ctx,
        provider=provider,
        config=orch_config,
    )

    _emit_review_result(result, base_ref=base_ref, head=head_sha)
    return 0 if result.ok else 1


async def _orchestrate_skip(provider: object, reason: str) -> ReviewResult:
    """Convenience wrapper to call run_review() with only a skip path."""
    from ai_pr_review.agents.dispatch import DispatchContext
    from ai_pr_review.orchestrate import run_review
    from ai_pr_review.vcs.protocol import DiffContext, VcsProvider

    assert isinstance(provider, VcsProvider)
    diff_path = Path("/tmp/ai-review-skip-diff.txt")
    diff_path.write_text("")
    ctx = DispatchContext(
        script_dir=Path("."),
        mode="full",
        diff_path=diff_path,
        provider="anthropic",
        standard_model="placeholder",
    )

    async def _no_llm(_req: object) -> object:
        raise RuntimeError("LLM call should not be invoked on skip path")

    return await run_review(
        diff=DiffContext(diff_text="", head_sha="0000000"),
        summary_text="",
        agents=[],
        llm_call=_no_llm,  # type: ignore[arg-type]
        dispatch_context=ctx,
        provider=provider,
        skip_reason=reason,
    )


def _make_changed_files(files: list[object]) -> ChangedFiles:
    """Build a ChangedFiles-like value for the gate evaluator.

    The compute payload may carry `changed_files` either as a list of path
    strings or a list of dicts (forward-compatible). Normalize and rebuild.
    """
    from ai_pr_review.manifest import build_changed_files

    paths: list[str] = []
    for entry in files:
        path = (
            str(entry.get("path") or "") if isinstance(entry, dict) else str(entry)
        )
        if path:
            paths.append(path)
    return build_changed_files(paths)


def _emit_review_result(result: ReviewResult, *, base_ref: str, head: str) -> None:
    """Emit a one-line summary to stderr."""
    if result.skipped:
        click.echo(f"Review skipped: {result.skip_reason}", err=True)
        return
    n_findings = len(result.findings)
    n_failed = len(result.failed_agents)
    click.echo(
        f"Review complete: {n_findings} findings, "
        f"{n_failed} failed agents, "
        f"event={result.outcome.event}, "
        f"base={base_ref[:7] if base_ref else '?'}..{head[:7] if head else '?'}",
        err=True,
    )


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
