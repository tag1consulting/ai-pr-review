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
from ai_pr_review.orchestrate import ReviewResult
from ai_pr_review.review.compute import run_compute
from ai_pr_review.vcs import ProviderConfigError

if TYPE_CHECKING:
    from ai_pr_review.llm.base import LLMRequest, LLMResponse
    from ai_pr_review.review.runtime import ReviewRuntime
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

    # Run issue-linker on first review in full mode when the VCS provider is GitHub.
    # rc.vcs_provider is the VCS provider (github/bitbucket/gitlab); rc.provider is
    # the AI provider (anthropic/openai/bedrock/etc). The guard here avoids a wasted
    # LLM call on non-GitHub repos because the prompt checks for "github" and returns
    # NONE immediately otherwise.
    # Fail-soft: if it returns NONE or errors, summary_text is unchanged.
    if not runtime.is_incremental and rc.review_mode == "full" and rc.vcs_provider == "github":
        issue_linker_md = await _run_issue_linker(
            manifest_text=runtime.manifest_text,
            base_ref=runtime.base_ref,
            script_dir=runtime.script_dir,
            provider=rc.vcs_provider,
            github_repository=rc.github_repository,
            model=rc.model_standard,
            llm_call=_llm_call,
        )
        if issue_linker_md:
            summary_text += "\n\n" + issue_linker_md

    def _token_renderer(
        successes: Sequence[_AgentResult], _sarif_elapsed_unused: float | None
    ) -> str:
        return _build_token_table_accordion(
            successes, runtime.sarif_elapsed_s, runtime.script_dir,
            effective_max_tokens=runtime.dispatch_context.max_tokens_per_agent,
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
                await _emit_telemetry_minimal(rc, outcome_override="dry_run",
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
        ),
    )

    if rc.telemetry_enabled:
        await _emit_telemetry(result, rc, runtime.feedback_entries_count, runtime.sarif_elapsed_s,
                              is_incremental=runtime.is_incremental)

    return 0 if result.ok else 1


async def _emit_telemetry(
    result: ReviewResult,
    config: ReviewConfig,
    feedback_entries_count: int,
    sarif_elapsed_s: float | None = None,
    *,
    is_incremental: bool = False,
    outcome_override: str = "",
) -> None:
    """Assemble and emit a telemetry event (fail-soft on any error)."""
    import datetime
    from collections import Counter

    from ai_pr_review.agents.dispatch import AgentResult as _AgentResult
    from ai_pr_review.agents.dispatch import FailedAgent as _FailedAgent
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
            outcome=outcome_override or result.outcome.event,
            findings_count=len(result.findings),
            findings_by_severity=findings_by_severity,
            failed_agents=[f.name for f in result.failed_agents],
            token_usage_by_agent=token_usage_by_agent,
            agent_latency_ms={ar.name: ar.elapsed_ms for ar in result.agent_results},
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
                for f in result.failed_agents
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


async def _emit_telemetry_minimal(
    config: ReviewConfig,
    *,
    outcome_override: str,
    is_incremental: bool = False,
) -> None:
    """Emit a minimal telemetry event when there are no agent results (skip/dry-run)."""
    import datetime

    from ai_pr_review.telemetry import TelemetryEvent, emit_telemetry
    try:
        telemetry_event = TelemetryEvent(
            correlation_id=os.environ.get("AI_PR_REVIEW_CORRELATION_ID", ""),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            repository=config.github_repository,
            pr_number=str(config.pr_number),
            outcome=outcome_override,
            findings_count=0,
            findings_by_severity={},
            failed_agents=[],
            token_usage_by_agent={},
            agent_latency_ms={},
            sarif_elapsed_s=None,
            learning_store_entries_loaded=0,
            telemetry_schema_version="2",
            provider=config.provider,
            model_standard=config.model_standard,
            model_premium=config.model_premium,
            review_mode=config.review_mode,
            is_incremental=is_incremental,
            failed_agent_latency_ms={},
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

    Returns response.text (the full LLM markdown including summary and
    walkthrough table) rather than SummarizerOutput.summary_md, because
    summary_md contains only the text before the **Type:** line — returning it
    alone would drop the walkthrough table.
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
        system_prompt = build_summarizer_system_prompt(prompt_path)

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
        parse_summarizer_output(response.text)
        return response.text
    except Exception as exc:
        logger.warning(
            "pr-summarizer: failed (review will continue without summary): %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        return _SUMMARIZER_FAILURE_NOTICE


async def _run_issue_linker(
    *,
    manifest_text: str,
    base_ref: str,
    script_dir: Path,
    provider: str,
    github_repository: str,
    model: str,
    llm_call: Callable[[LLMRequest], Awaitable[LLMResponse]],
) -> str:
    """Run the issue-linker agent and return its markdown output, or "" to suppress.

    ``provider`` is the VCS provider (github/bitbucket/gitlab), NOT the AI provider.
    The issue-linker is GitHub-only. When the provider is not github or the agent
    returns the sentinel NONE, this function returns "" so the caller skips
    appending to summary_text. Any error is logged as WARNING and "" is returned
    (fail-soft).

    The user message provides the context the agent needs: commit log, branch name,
    file manifest, repository slug, and PROVIDER. The agent uses these along with
    gh CLI calls to discover and assess related issues.
    """
    from ai_pr_review.llm.base import LLMRequest

    try:
        prompt_path = script_dir / "prompts" / "issue-linker.md"
        if not prompt_path.exists():
            logger.warning("issue-linker: prompt not found at %s; skipping", prompt_path)
            return ""
        system_prompt = prompt_path.read_text()

        commit_log = ""
        _git_cmd = ["git", "log", "--format=%h %s%n%b", "--max-count=20", f"origin/{base_ref}..HEAD"]
        try:
            proc = await anyio.to_thread.run_sync(
                lambda: subprocess.run(
                    _git_cmd, capture_output=True, text=True, timeout=15,
                )
            )
        except subprocess.TimeoutExpired:
            logger.warning("issue-linker: git log timed out; proceeding without commit log")
        except Exception as exc:
            logger.warning("issue-linker: could not get commit log: %s", exc)
        else:
            if proc.returncode == 0:
                commit_log = proc.stdout.strip()
            else:
                commit_log = "_Note: commit log unavailable (git log failed)._"

        branch_name = ""
        try:
            branch_proc = await anyio.to_thread.run_sync(
                lambda: subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                )
            )
            if branch_proc.returncode == 0:
                branch_name = branch_proc.stdout.strip()
        except Exception as exc:
            logger.warning("issue-linker: could not get branch name: %s", exc)

        user_message = (
            f"PROVIDER: {provider}\n\n"
            f"REPOSITORY: {github_repository}\n\n"
            f"## Branch Name\n\n{branch_name or '(unavailable)'}\n\n"
            f"## Commit Log\n\n{commit_log or '(unavailable)'}\n\n"
            f"## File Manifest\n\n{manifest_text}\n"
        )

        request = LLMRequest(
            model_id=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=4096,
        )
        response: LLMResponse = await llm_call(request)
        text = response.text.strip()
        if text == "NONE" or not text:
            logger.debug("issue-linker: returned NONE or empty; skipping")
            return ""
        return text
    except Exception as exc:
        logger.warning(
            "issue-linker: failed (review will continue without issue links): %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        return ""


def _build_token_table_accordion(
    successes: Sequence[object],
    sarif_elapsed_s: float | None,
    script_dir: Path,
    *,
    effective_max_tokens: int = 0,
) -> str:
    """Return a <details>-wrapped token cost table string, or "" on no-data/error.

    Designed to be used as the ``token_table_renderer`` callback passed to
    ``run_review()``.  All exceptions are caught and logged as WARNING so token
    table failure never aborts a review.

    ``effective_max_tokens`` is the user-configured cap from
    ``DispatchContext.max_tokens_per_agent`` (i.e. ``AI_MAX_TOKENS_PER_AGENT``).
    When > 0 it overrides the per-agent roster default so the table reflects
    the actual cap sent to the LLM rather than the hard-coded roster value.
    """
    from ai_pr_review.agents.dispatch import AgentResult
    from ai_pr_review.agents.roster import AGENTS
    from ai_pr_review.pricing import TokenEntry, emit_token_table, load_pricing

    _roster_max_by_name = {spec.name: spec.max_output_tokens for spec in AGENTS}
    token_log: list[TokenEntry] = []
    for ar in successes:
        if isinstance(ar, AgentResult) and ar.token_log is not None:
            tl = ar.token_log
            # Use the effective user-configured cap when set; fall back to the
            # per-agent roster default so the table matches what was actually sent.
            cap = effective_max_tokens if effective_max_tokens > 0 else _roster_max_by_name.get(ar.name, 0)
            token_log.append(TokenEntry(
                agent=ar.name,
                model=tl.model,
                input_tokens=tl.input,
                output_tokens=tl.output,
                cache_creation_tokens=tl.cache_creation,
                cache_read_tokens=tl.cache_read,
                max_output_tokens=cap,
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


def _write_step_summary(
    result: ReviewResult,
    runtime: ReviewRuntime,
    summary_text: str,
    token_table_md: str = "",
) -> None:
    """Write a concise run summary to GITHUB_STEP_SUMMARY when available.

    Mirrors the bash engine's Phase 4 step-summary block. Fail-soft: any
    error is logged as WARNING and the review result is unaffected.

    ``token_table_md`` should be the pre-built accordion string from
    ``_build_token_table_accordion()`` so the step summary matches the PR
    comment exactly and avoids a second pricing-file read.
    """
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not step_summary_path:
        return
    try:
        cf = runtime.changed_files
        rc = runtime.config
        languages = ", ".join(cf.languages) if cf.languages else "none detected"
        file_count = len(cf.all_files)
        n_findings = len(result.findings)
        n_failed = len(result.failed_agents)
        failed_line = (
            f"\n**Failed agents:** {', '.join(f.name for f in result.failed_agents)}"
            if result.failed_agents else ""
        )

        token_section = (
            f"\n### Token Usage\n\n{token_table_md}\n\n"
            "_Prices are public list rates and do not reflect discounts, "
            "commitments, or proxy markups._\n"
            if token_table_md else ""
        )

        lines = [
            "## AI PR Review Results",
            "",
            f"**Mode:** {rc.review_mode} | **Files:** {file_count}",
            f"**Languages:** {languages}",
            f"**Agents:** {len(runtime.agents)} finding agents",
        ]
        if failed_line:
            lines.append(failed_line.lstrip())
        lines += [
            "",
            f"**Findings:** {n_findings}"
            + (f" ({n_failed} agent(s) failed)" if n_failed else ""),
            "",
        ]
        if token_section:
            lines.append(token_section)
        if summary_text.strip():
            lines += ["### Summary", "", summary_text.strip(), ""]

        content = "\n".join(lines)
        with open(step_summary_path, "a", encoding="utf-8") as fh:
            fh.write(content + "\n")
    except Exception as exc:
        logger.warning(
            "step summary: unexpected error building/writing step summary: %s", exc, exc_info=True
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
