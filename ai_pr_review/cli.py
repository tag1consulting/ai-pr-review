"""CLI entry point for the AI PR Review Python engine.

Subcommands:
- `compute`: run the compute phase only, writing a JSON payload to
  AI_PR_REVIEW_COMPUTE_OUTPUT. Retained for backwards compatibility.
- `review`: end-to-end pipeline (compute -> dispatch agents ->
  extract findings -> outcome -> post via VcsProvider).
- `slash`: handle one ``/ai-pr-review`` comment command.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import click
from pydantic import ValidationError

from ai_pr_review.config import ConfigError, ReviewConfig
from ai_pr_review.logging import generate_correlation_id, setup_logging
from ai_pr_review.orchestrate import ReviewResult
from ai_pr_review.review.compute import run_compute
from ai_pr_review.review.preflight import run_issue_linker as _run_issue_linker
from ai_pr_review.review.preflight import run_summarizer as _run_summarizer
from ai_pr_review.review.reporting import (
    build_token_table_accordion as _build_token_table_accordion,
)
from ai_pr_review.review.reporting import emit_review_result as _emit_review_result
from ai_pr_review.review.reporting import write_step_summary as _write_step_summary
from ai_pr_review.vcs import ProviderConfigError

if TYPE_CHECKING:
    from ai_pr_review.llm.base import LLMRequest, LLMResponse
    from ai_pr_review.vcs.protocol import VcsProvider

logger = logging.getLogger(__name__)


def _secret_set(config: ReviewConfig) -> frozenset[str]:
    """Return non-empty credential values from config for Layer 3 secret masking."""
    return frozenset(
        v for v in (
            config.anthropic_api_key,
            config.openai_api_key,
            config.google_api_key,
            config.bedrock_api_key,
            config.gh_token,
            config.gitlab_token,
            config.bitbucket_api_token,
            config.ci_job_token,
        )
        if v
    )


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
    """Run the compute phase (diff, manifest, findings) and write handoff JSON."""
    try:
        config = ReviewConfig.from_env()
    except (ConfigError, ValidationError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    correlation_id = os.environ.get("AI_PR_REVIEW_CORRELATION_ID") or generate_correlation_id()
    os.environ["AI_PR_REVIEW_CORRELATION_ID"] = correlation_id
    setup_logging(config.log_format, config.log_level, correlation_id, secrets=_secret_set(config))

    payload = run_compute(config)

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
    """Run the end-to-end review pipeline.

    Builds the VCS provider, runs compute -> dispatch -> post in one process.
    Reads configuration from environment variables (PR_NUMBER, BASE_REF,
    HEAD_SHA, GITHUB_REPOSITORY, etc.) and VCS_PROVIDER for selecting the
    posting target.

    Exit codes:
      0 — review posted successfully (or skipped cleanly)
      1 — configuration / posting error
      2 — review posted but outcome is REQUEST_CHANGES or COMMENT (when AI_FAIL_ON_FINDINGS=true)
    """
    try:
        config = ReviewConfig.from_env()
    except (ConfigError, ValidationError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    correlation_id = os.environ.get("AI_PR_REVIEW_CORRELATION_ID") or generate_correlation_id()
    os.environ["AI_PR_REVIEW_CORRELATION_ID"] = correlation_id
    setup_logging(config.log_format, config.log_level, correlation_id, secrets=_secret_set(config))
    logger.info("review started")

    try:
        exit_code = anyio.run(_run_review_async, config)
    except ProviderConfigError as exc:
        logger.error("Provider configuration error: %s", exc)
        logger.debug("Provider configuration error detail", exc_info=True)
        click.echo(f"Provider configuration error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — top-level catch for clean exit
        import traceback
        logger.error("Unexpected error in review pipeline", exc_info=True)
        click.echo(f"ERROR: {exc!r}", err=True)
        click.echo(traceback.format_exc(), err=True)
        sys.exit(1)
    sys.exit(exit_code)


async def _run_review_async(config: ReviewConfig) -> int:
    """Execute the review pipeline; return CLI exit code."""
    from ai_pr_review.agents.dispatch import AgentResult as _AgentResult
    from ai_pr_review.llm.client import call_llm
    from ai_pr_review.orchestrate import run_review
    from ai_pr_review.review.runtime import SkipPlan, build_review_runtime

    runtime = await build_review_runtime(config)

    if isinstance(runtime, SkipPlan):
        click.echo(f"Skipping review: {runtime.reason}", err=True)
        result = await _orchestrate_skip(runtime.provider, runtime.reason, config=config.resolve_models())
        if config.telemetry_enabled:
            try:
                resolved_cfg = config.resolve_models()
            except Exception:
                resolved_cfg = config
            await _emit_telemetry(result, resolved_cfg, 0, outcome_override="skipped")
        return 0 if result.ok else 1

    # Use the post-resolve_models() config stored on the runtime for all downstream use.
    rc = runtime.config

    # Bind LLM call to the configured provider.
    async def _llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, rc.provider)

    from ai_pr_review.agents.roster import agent_allowed as _agent_allowed

    # Run pr-summarizer on first review (fail-soft; skip on incremental runs).
    # Also skip if the consumer has excluded it via the agents denylist or allowlist.
    summary_text = runtime.summary_prefix
    if not runtime.is_incremental and _agent_allowed("pr-summarizer", rc.agents, rc.exclude_agents):
        summary_text += await _run_summarizer(
            diff_text=runtime.diff.diff_text,
            manifest_text=runtime.manifest_text,
            base_ref=runtime.base_ref,
            script_dir=runtime.script_dir,
            model=rc.model_standard,
            temperature=rc.temperature,
            llm_call=_llm_call,
        )

    # Run issue-linker on first review in full mode when the VCS provider is GitHub.
    # rc.vcs_provider is the VCS provider (github/bitbucket/gitlab); rc.provider is
    # the AI provider (anthropic/openai/bedrock/etc). The guard here avoids a wasted
    # LLM call on non-GitHub repos because the prompt checks for "github" and returns
    # NONE immediately otherwise.
    # Also skip if the consumer has excluded it via the agents denylist or allowlist.
    # Fail-soft: if it returns NONE or errors, summary_text is unchanged.
    if (
        not runtime.is_incremental
        and rc.review_mode == "full"
        and rc.vcs_provider == "github"
        and _agent_allowed("issue-linker", rc.agents, rc.exclude_agents)
    ):
        issue_linker_md = await _run_issue_linker(
            manifest_text=runtime.manifest_text,
            base_ref=runtime.base_ref,
            script_dir=runtime.script_dir,
            provider=rc.vcs_provider,
            github_repository=rc.github_repository,
            model=rc.model_standard,
            temperature=rc.temperature,
            llm_call=_llm_call,
        )
        if issue_linker_md:
            summary_text += "\n\n" + issue_linker_md

    def _token_renderer(
        successes: Sequence[_AgentResult],
        _sarif_elapsed_unused: float | None,
        judge_input_tokens: int,
        judge_output_tokens: int,
        judge_cache_creation_tokens: int,
        judge_cache_read_tokens: int,
        judge_model: str,
    ) -> str:
        return _build_token_table_accordion(
            successes, runtime.sarif_elapsed_s, runtime.script_dir,
            effective_max_tokens=runtime.dispatch_context.max_tokens_per_agent,
            judge_input_tokens=judge_input_tokens,
            judge_output_tokens=judge_output_tokens,
            judge_cache_creation_tokens=judge_cache_creation_tokens,
            judge_cache_read_tokens=judge_cache_read_tokens,
            judge_model=judge_model,
        )

    # Honour AI_DRY_RUN — assemble is complete but skip VCS posting.
    if rc.dry_run:
        click.echo("[dry-run] review complete — VCS posting suppressed", err=True)
        click.echo(
            f"[dry-run] diff: {len(runtime.diff.diff_text.splitlines())} lines, "
            f"{len(runtime.agents)} agents selected"
        )
        click.echo(f"[dry-run] summary_text length: {len(summary_text)} chars")
        if rc.telemetry_enabled:
            try:
                await _emit_telemetry(None, rc, outcome_override="dry_run",
                                      is_incremental=runtime.is_incremental)
            except Exception as _tel_exc:
                logger.warning("[ai-pr-review] dry-run telemetry failed: %s", _tel_exc)
        return 0

    result = await run_review(
        diff=runtime.diff,
        summary_text=summary_text,
        agents=runtime.agents,
        llm_call=_llm_call,
        dispatch_context=runtime.dispatch_context,
        provider=runtime.provider,
        config=runtime.orch_config,
        token_table_renderer=_token_renderer,
    )

    _emit_review_result(result, base_ref=runtime.base_ref, head=runtime.head_sha)
    _write_step_summary(
        result, runtime, summary_text,
        token_table_md=_build_token_table_accordion(
            result.agent_results, runtime.sarif_elapsed_s, runtime.script_dir,
            effective_max_tokens=runtime.dispatch_context.max_tokens_per_agent,
            judge_input_tokens=result.judge_input_tokens,
            judge_output_tokens=result.judge_output_tokens,
            judge_cache_creation_tokens=result.judge_cache_creation_tokens,
            judge_cache_read_tokens=result.judge_cache_read_tokens,
            judge_model=result.judge_model,
        ),
    )

    if rc.telemetry_enabled:
        await _emit_telemetry(result, rc, runtime.feedback_entries_count, runtime.sarif_elapsed_s,
                              is_incremental=runtime.is_incremental)

    if not result.ok:
        return 1
    if rc.fail_on_findings and result.outcome.event in ("REQUEST_CHANGES", "COMMENT"):
        return 2
    return 0


async def _emit_telemetry(
    result: ReviewResult | None,
    config: ReviewConfig,
    feedback_entries_count: int = 0,
    sarif_elapsed_s: float | None = None,
    *,
    is_incremental: bool = False,
    outcome_override: str = "",
) -> None:
    """Assemble and emit a telemetry event (fail-soft on any error).

    When ``result`` is None (skip or dry-run paths), all agent-dependent fields
    default to zero/empty so a single call site covers all three paths.
    """
    import datetime
    from collections import Counter

    from ai_pr_review.agents.dispatch import AgentResult as _AgentResult
    from ai_pr_review.agents.dispatch import FailedAgent as _FailedAgent
    from ai_pr_review.telemetry import TelemetryEvent, emit_telemetry

    try:
        token_usage_by_agent: dict[str, dict[str, object]] = {}
        agent_results = result.agent_results if result is not None else []
        failed_agents = result.failed_agents if result is not None else []
        findings = result.findings if result is not None else []

        for ar in agent_results:
            if isinstance(ar, _AgentResult) and ar.token_log is not None:
                tl = ar.token_log
                token_usage_by_agent[ar.name] = {
                    "input": tl.input,
                    "output": tl.output,
                    "cache_creation": tl.cache_creation,
                    "cache_read": tl.cache_read,
                    "model": tl.model,
                }

        findings_by_severity: dict[str, int] = dict(Counter(f.severity for f in findings))
        outcome = outcome_override or (result.outcome.event if result is not None else "")

        telemetry_event = TelemetryEvent(
            correlation_id=os.environ.get("AI_PR_REVIEW_CORRELATION_ID", ""),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            repository=config.github_repository,
            pr_number=str(config.pr_number),
            outcome=outcome,
            findings_count=len(findings),
            findings_by_severity=findings_by_severity,
            failed_agents=[f.name for f in failed_agents],
            token_usage_by_agent=token_usage_by_agent,
            agent_latency_ms={ar.name: ar.elapsed_ms for ar in agent_results},
            sarif_elapsed_s=sarif_elapsed_s,
            learning_store_entries_loaded=feedback_entries_count,
            telemetry_schema_version="2",
            provider=config.provider,
            model_standard=config.model_standard,
            model_premium=config.model_premium,
            review_mode=config.review_mode,
            is_incremental=is_incremental,
            failed_agent_latency_ms={
                f.name: f.elapsed_ms
                for f in failed_agents
                if isinstance(f, _FailedAgent)
            },
        )
    except Exception as exc:
        logger.warning("[ai-pr-review] telemetry assembly failed: %s", exc, exc_info=True)
        return
    try:
        await anyio.to_thread.run_sync(
            lambda: emit_telemetry(telemetry_event, sink=config.telemetry_sink)
        )
    except Exception as exc:
        logger.warning("[ai-pr-review] telemetry emission failed: %s", exc, exc_info=True)


async def _orchestrate_skip(
    provider: VcsProvider, reason: str, *, config: ReviewConfig
) -> ReviewResult:
    """Convenience wrapper to call run_review() with only a skip path."""
    from ai_pr_review.agents.dispatch import DispatchContext
    from ai_pr_review.orchestrate import run_review
    from ai_pr_review.vcs.protocol import DiffContext

    diff_path = Path("/tmp/ai-review-skip-diff.txt")
    diff_path.write_text("", encoding="utf-8")
    ctx = DispatchContext(
        script_dir=Path("."),
        mode=config.review_mode,
        diff_path=diff_path,
        provider=config.provider,
        standard_model=config.model_standard,
        premium_model=config.model_premium,
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



@cli.command()
@click.option(
    "--body",
    envvar="SLASH_COMMENT_BODY",
    default="",
    help="Raw comment body (defaults to SLASH_COMMENT_BODY env var).",
)
@click.option(
    "--source",
    envvar="SLASH_SOURCE",
    default="",
    help="Finding source tag (e.g. 'code-reviewer', 'sarif:bandit').",
)
@click.option(
    "--file",
    "file_path",
    envvar="SLASH_FILE",
    default="",
    help="File path the finding was on (may be empty).",
)
@click.option(
    "--rule-id",
    envvar="SLASH_RULE_ID",
    default="",
    help="Rule ID from the original finding (may be empty).",
)
@click.option(
    "--context-missing-reason",
    envvar="SLASH_CONTEXT_MISSING_REASON",
    default="",
    help="Why finding context could not be extracted (forwarded from GHA workflow output).",
)
def slash(body: str, source: str, file_path: str, rule_id: str, context_missing_reason: str) -> None:
    """Handle one /ai-pr-review comment command.

    Parses the comment body, dispatches to the appropriate handler, and
    prints the reply message to stdout.  The GitHub Actions step in
    slash-commands.yml captures stdout and posts it as a reply comment.

    Exit codes:
      0 — handled (or no-op / not a slash command)
      2 — parse error (unknown command / malformed)
    """
    from ai_pr_review.slash.handlers import build_entry, handle_command
    from ai_pr_review.slash.parser import ParseError, parse_command

    if not body:
        # Nothing to do — not an error
        return

    result = parse_command(body)

    if result is None:
        # Not a slash command — silently ignore
        return

    if isinstance(result, ParseError):
        click.echo(f"slash: {result.message}", err=True)
        sys.exit(2)

    try:
        config = ReviewConfig.from_env()
    except (ConfigError, ValidationError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    correlation_id = os.environ.get("AI_PR_REVIEW_CORRELATION_ID") or generate_correlation_id()
    os.environ["AI_PR_REVIEW_CORRELATION_ID"] = correlation_id
    setup_logging(config.log_format, config.log_level, correlation_id, secrets=_secret_set(config))

    from ai_pr_review.feedback.store import make_store

    store = make_store(config)

    # Detect missing context: both source and file are empty for a feedback
    # command.  The entry is still persisted (captures reviewer intent) but
    # flagged in extras so the learning-loop ranker can identify low-fidelity
    # records and operators can audit them.  A loud warning is emitted so the
    # issue surfaces in workflow logs.
    context_missing = (
        result.is_feedback_command
        and not source
        and not file_path
        and result.finding_id is None
    )
    if context_missing:
        logger.warning(
            "slash: persisting feedback entry with no finding context "
            "(source and file are both empty); command=%r reason=%r%s",
            result.canonical_name,
            result.reason,
            f" context_missing_reason={context_missing_reason!r}" if context_missing_reason else "",
        )

    entry = build_entry(
        result,
        source=source,
        file=file_path,
        rule_id=rule_id,
        context_missing=context_missing,
        context_missing_reason=context_missing_reason,
    )
    reply = handle_command(result, entry, store)
    if reply:
        click.echo(reply)
