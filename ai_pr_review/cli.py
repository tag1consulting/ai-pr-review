"""CLI entry point for the AI PR Review Python engine.

Subcommands:
- `compute`: run the compute phase only, writing a JSON payload to
  AI_PR_REVIEW_COMPUTE_OUTPUT. Retained for backwards compatibility.
- `review`: end-to-end Python pipeline (compute -> dispatch agents ->
  extract findings -> outcome -> post via VcsProvider). Replaces the
  bash post-review scripts when AI_PR_REVIEW_ENGINE=python.
- `slash`: handle one ``/ai-pr-review`` comment command.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import click

from ai_pr_review.config import ConfigError, ReviewConfig
from ai_pr_review.logging import generate_correlation_id, setup_logging
from ai_pr_review.manifest import ChangedFiles
from ai_pr_review.orchestrate import ReviewResult
from ai_pr_review.review.compute import run_compute
from ai_pr_review.vcs import ProviderConfigError

if TYPE_CHECKING:
    from ai_pr_review.llm.base import LLMRequest, LLMResponse

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
    except ConfigError as exc:
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

    runtime = build_review_runtime(config)

    if isinstance(runtime, SkipPlan):
        click.echo(f"Skipping review: {runtime.reason}", err=True)
        result = await _orchestrate_skip(runtime.provider, runtime.reason, config=config)
        return 0 if result.ok else 1

    # Use the post-resolve_models() config stored on the runtime for all downstream use.
    rc = runtime.config

    # Bind LLM call to the configured provider.
    async def _llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, rc.provider)

    # Run pr-summarizer on first review (fail-soft; skip on incremental runs).
    summary_text = runtime.summary_prefix
    if not runtime.is_incremental:
        summary_text += await _run_summarizer(
            diff_text=runtime.diff.diff_text,
            manifest_text=runtime.manifest_text,
            base_ref=runtime.base_ref,
            script_dir=runtime.script_dir,
            model=rc.model_standard,
            llm_call=_llm_call,
        )

    def _token_renderer(
        successes: Sequence[_AgentResult], sarif_elapsed_s: float | None
    ) -> str:
        return _build_token_table_accordion(successes, sarif_elapsed_s, runtime.script_dir)

    # Honour AI_DRY_RUN — assemble is complete but skip VCS posting.
    if rc.dry_run:
        click.echo("[dry-run] review complete — VCS posting suppressed", err=True)
        click.echo(
            f"[dry-run] diff: {len(runtime.diff.diff_text.splitlines())} lines, "
            f"{len(runtime.agents)} agents selected"
        )
        click.echo(f"[dry-run] summary_text length: {len(summary_text)} chars")
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

    if rc.telemetry_enabled:
        await _emit_telemetry(result, rc, runtime.feedback_entries_count)

    return 0 if result.ok else 1


async def _emit_telemetry(
    result: ReviewResult,
    config: ReviewConfig,
    feedback_entries_count: int,
) -> None:
    """Assemble and emit a telemetry event (fail-soft on any error)."""
    import datetime
    from collections import Counter

    from ai_pr_review.agents.dispatch import AgentResult as _AgentResult
    from ai_pr_review.telemetry import TelemetryEvent, emit_telemetry

    try:
        token_usage_by_agent: dict[str, dict[str, object]] = {}
        for ar in result.agent_results:
            if isinstance(ar, _AgentResult) and ar.token_log is not None:
                tl = ar.token_log
                token_usage_by_agent[ar.name] = {
                    "input": tl.input,
                    "output": tl.output,
                    "cache_creation": tl.cache_creation,
                    "cache_read": tl.cache_read,
                    "model": tl.model,
                }

        findings_by_severity: dict[str, int] = dict(Counter(f.severity for f in result.findings))

        telemetry_event = TelemetryEvent(
            correlation_id=os.environ.get("AI_PR_REVIEW_CORRELATION_ID", ""),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            repository=config.github_repository,
            pr_number=str(config.pr_number),
            outcome=result.outcome.event,
            findings_count=len(result.findings),
            findings_by_severity=findings_by_severity,
            failed_agents=[f.name for f in result.failed_agents],
            token_usage_by_agent=token_usage_by_agent,
            agent_latency_ms={ar.name: ar.elapsed_ms for ar in result.agent_results},
            sarif_elapsed_s=result.sarif_elapsed_s,
            learning_store_entries_loaded=feedback_entries_count,
            telemetry_schema_version="1",
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
    provider: object, reason: str, *, config: ReviewConfig
) -> ReviewResult:
    """Convenience wrapper to call run_review() with only a skip path."""
    from ai_pr_review.agents.dispatch import DispatchContext
    from ai_pr_review.orchestrate import run_review
    from ai_pr_review.vcs.protocol import DiffContext, VcsProvider

    if not isinstance(provider, VcsProvider):
        raise TypeError(f"Expected VcsProvider, got {type(provider).__name__}")
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
        else:
            logger.warning("Skipping malformed changed_files entry: %r", entry)
    return build_changed_files(paths)


_SUMMARIZER_FAILURE_NOTICE = "> ⚠️ PR summary generation failed — see CI logs.\n\n"


async def _run_summarizer(
    *,
    diff_text: str,
    manifest_text: str,
    base_ref: str,
    script_dir: Path,
    model: str,
    llm_call: Callable[[LLMRequest], Awaitable[LLMResponse]],
) -> str:
    """Run the pr-summarizer agent and return its formatted markdown output.

    Returns response.text (the full LLM markdown including summary, walkthrough
    table, and optional sequence diagram) rather than SummarizerOutput.summary_md,
    because summary_md contains only the text before the **Type:** line —
    returning it alone would drop the walkthrough table and sequence diagram.
    parse_summarizer_output() is called for validation only (not to reformat).

    Fail-soft: on any error logs a WARNING and returns _SUMMARIZER_FAILURE_NOTICE
    so the PR comment communicates the partial failure rather than silently
    omitting the summary.  except Exception is intentional: the fail-soft contract
    requires that any unexpected error (KeyError, TypeError, etc.) in the prompt
    assembly or parse path skips the summary rather than aborting the whole review.
    """
    from ai_pr_review.agents.summarizer import (
        build_summarizer_system_prompt,
        build_summarizer_user_message,
        parse_summarizer_output,
    )
    from ai_pr_review.llm.base import LLMRequest

    try:
        prompt_path = script_dir / "prompts" / "pr-summarizer.md"
        system_prompt = build_summarizer_system_prompt(prompt_path, include_diagram=True)

        commit_log = ""
        _git_cmd = ["git", "log", "--format=%h %s%n%b", "--max-count=20", f"origin/{base_ref}..HEAD"]
        proc: subprocess.CompletedProcess[str] | None = None
        try:
            # Run in a thread to avoid blocking the anyio event loop.
            proc = await anyio.to_thread.run_sync(
                lambda: subprocess.run(
                    _git_cmd, capture_output=True, text=True, timeout=15,
                )
            )
        except subprocess.TimeoutExpired:
            logger.warning("pr-summarizer: git log timed out; proceeding without commit log")
        except Exception as exc:
            logger.warning("pr-summarizer: could not get commit log: %s", exc)
        else:
            if proc.returncode != 0:
                logger.warning(
                    "pr-summarizer: git log exited %d; stderr=%r stdout=%r",
                    proc.returncode, proc.stderr.strip()[:500], proc.stdout.strip()[:500],
                )
                # Use a prompt-safe marker so the LLM knows context is absent.
                commit_log = "_Note: commit log unavailable (git log failed)._"
            else:
                commit_log = proc.stdout.strip()

        user_message = build_summarizer_user_message(manifest_text, commit_log, diff_text)
        request = LLMRequest(
            model_id=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=4096,
        )
        response: LLMResponse = await llm_call(request)
        # parse_summarizer_output returns a SummarizerOutput dataclass (not a
        # cleaned string). We call it here only to validate the response is
        # parseable before returning the raw markdown to the caller.
        logger.debug("pr-summarizer: raw response length=%d chars", len(response.text))
        parse_summarizer_output(response.text, include_diagram=True)
        return response.text
    except Exception as exc:
        logger.warning(
            "pr-summarizer: failed (review will continue without summary): %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        return _SUMMARIZER_FAILURE_NOTICE


def _build_token_table_accordion(
    successes: Sequence[object],
    sarif_elapsed_s: float | None,
    script_dir: Path,
) -> str:
    """Return a <details>-wrapped token cost table string, or "" on no-data/error.

    Designed to be used as the ``token_table_renderer`` callback passed to
    ``run_review()``.  All exceptions are caught and logged as WARNING so token
    table failure never aborts a review.
    """
    from ai_pr_review.agents.dispatch import AgentResult
    from ai_pr_review.agents.roster import AGENTS
    from ai_pr_review.pricing import TokenEntry, emit_token_table, load_pricing

    _max_tokens_by_name = {spec.name: spec.max_output_tokens for spec in AGENTS}
    token_log: list[TokenEntry] = []
    for ar in successes:
        if isinstance(ar, AgentResult) and ar.token_log is not None:
            tl = ar.token_log
            token_log.append(TokenEntry(
                agent=ar.name,
                model=tl.model,
                input_tokens=tl.input,
                output_tokens=tl.output,
                cache_creation_tokens=tl.cache_creation,
                cache_read_tokens=tl.cache_read,
                max_output_tokens=_max_tokens_by_name.get(ar.name, 0),
            ))

    if not token_log:
        return ""

    # All enriched agents receive the same context block; take the max (which
    # equals the single non-zero value) rather than summing to avoid double-counting.
    context_tokens = max(
        (ar.context_tokens_used for ar in successes if isinstance(ar, AgentResult)),
        default=0,
    )

    pricing_file = str(script_dir / "config" / "model-pricing.json")
    try:
        pricing_data = load_pricing(pricing_file)
    except Exception as exc:
        logger.warning(
            "token table: could not load pricing file %r: %s", pricing_file, exc, exc_info=True,
        )
        return ""

    try:
        table = emit_token_table(
            token_log,
            pricing_data,
            context_tokens=context_tokens,
            sarif_elapsed_s=sarif_elapsed_s,
        )
    except Exception as exc:
        logger.warning(
            "token table: could not render table (pricing_file=%r): %s",
            pricing_file, exc, exc_info=True,
        )
        return ""

    return (
        "<details>\n<summary>Token usage by agent</summary>\n\n"
        + table
        + "\n</details>"
    )


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
def slash(body: str, source: str, file_path: str, rule_id: str) -> None:
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
    except ConfigError as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    correlation_id = os.environ.get("AI_PR_REVIEW_CORRELATION_ID") or generate_correlation_id()
    os.environ["AI_PR_REVIEW_CORRELATION_ID"] = correlation_id
    setup_logging(config.log_format, config.log_level, correlation_id, secrets=_secret_set(config))

    from ai_pr_review.feedback.store import make_store

    store = make_store(config)
    entry = build_entry(result, source=source, file=file_path, rule_id=rule_id)
    reply = handle_command(result, entry, store)
    if reply:
        click.echo(reply)
