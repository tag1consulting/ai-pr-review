"""CLI entry point for the AI PR Review Python engine.

Subcommands:
- `compute`: run the compute phase only, writing a JSON payload to
  AI_PR_REVIEW_COMPUTE_OUTPUT. Retained for backwards compatibility with
  the Epic 1 handoff path.
- `review`: end-to-end Python pipeline (compute -> dispatch agents ->
  extract findings -> outcome -> post via VcsProvider). Replaces the
  bash post-review scripts when AI_PR_REVIEW_ENGINE=python.
- `slash`: handle one ``/ai-pr-review`` comment command (Capability C).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import click

from ai_pr_review.config import ConfigError, ReviewConfig
from ai_pr_review.manifest import ChangedFiles
from ai_pr_review.orchestrate import ReviewResult
from ai_pr_review.vcs import ProviderConfigError

if TYPE_CHECKING:
    from ai_pr_review.llm.base import LLMRequest, LLMResponse
    from ai_pr_review.vcs.protocol import VcsProvider

logger = logging.getLogger(__name__)


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
    from ai_pr_review.agents.dispatch import DispatchContext
    from ai_pr_review.agents.gates import evaluate_gates, filter_agents
    from ai_pr_review.agents.roster import AGENTS
    from ai_pr_review.llm.client import call_llm
    from ai_pr_review.orchestrate import OrchestrationConfig, run_review
    from ai_pr_review.vcs import provider_from_env
    from ai_pr_review.vcs.protocol import DiffContext, VcsProvider

    # 1. Build provider from VCS_PROVIDER env
    provider = provider_from_env()
    if not isinstance(provider, VcsProvider):
        raise TypeError(f"Expected VcsProvider, got {type(provider).__name__}")

    # 2. Run compute phase to get diff + manifest
    payload = _run_compute(config)
    if payload.get("skip"):
        reason = str(payload.get("reason") or "no changes")
        click.echo(f"Skipping review: {reason}", err=True)
        result = await _orchestrate_skip(provider, reason, config=config)
        return 0 if result.ok else 1

    diff_text = str(payload.get("diff") or "")
    head_sha = str(payload.get("head") or config.head_sha)
    base_ref = str(payload.get("base") or config.base_ref)

    # 3. Build summary prefix. Prepend merge-filter fallback note when present.
    merge_filter_fallback = str(payload.get("merge_filter_fallback_reason") or "")
    summary_text = ""
    if merge_filter_fallback:
        summary_text = (
            f"_Note: merge-commit filtering was skipped ({merge_filter_fallback}); "
            "diff may include upstream changes._\n\n"
        )
    is_incremental = bool(payload.get("is_incremental"))

    # 4. Build feedback addendum when AI_FEEDBACK_LOOP=1 (before dispatch context)
    feedback_addendum = ""
    if config.enable_feedback_loop:
        try:
            from ai_pr_review.feedback.inject import build_feedback_addendum
            from ai_pr_review.feedback.store import make_store
            store = make_store(config)
            entries = store.load_recent()
            feedback_addendum = build_feedback_addendum(
                entries, diff_text, max_tokens=config.feedback_max_tokens
            )
        except Exception as exc:
            logger.warning(
                "feedback loop: could not load feedback store: %s", exc, exc_info=True
            )

    # 5. Build dispatch context
    # AI_PR_REVIEW_SCRIPT_DIR is exported by review.sh so the Python engine
    # can locate prompts/language-profiles when installed as a pip package.
    _env_script_dir = os.environ.get("AI_PR_REVIEW_SCRIPT_DIR")
    script_dir = Path(_env_script_dir) if _env_script_dir else Path(__file__).resolve().parent.parent
    diff_path = Path(os.environ.get("AI_PR_REVIEW_DIFF_FILE") or "/tmp/ai-review-diff.txt")
    diff_path.write_text(diff_text, encoding="utf-8")

    _raw_changed = payload.get("changed_files")
    _changed_list: list[str] = [str(f) for f in _raw_changed if f] if isinstance(_raw_changed, list) else []

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
        enable_context_enrichment=config.enable_context_enrichment,
        context_max_tokens=config.context_max_tokens,
        context_lookup_lines=config.context_lookup_lines,
        repo_root=Path("."),
        changed_files=_changed_list,
        feedback_addendum=feedback_addendum,
    )

    # 6. Apply conditional gates
    last_reviewed = provider.get_last_reviewed_sha()
    raw_paths = payload.get("changed_files")
    changed_paths: list[object] = raw_paths if isinstance(raw_paths, list) else []
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

    # 7. Bind LLM call to the configured provider
    async def _llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, config.provider)

    # 7.5. Run pr-summarizer on first review (fail-soft; skip on incremental runs).
    if not is_incremental:
        summary_text += await _run_summarizer(
            diff_text=diff_text,
            manifest_text=str(payload.get("manifest") or ""),
            base_ref=base_ref,
            script_dir=script_dir,
            model=config.model_standard,
            llm_call=_llm_call,
        )

    # 8. Run the orchestrator
    orch_config = OrchestrationConfig(
        mode=config.review_mode,  # type: ignore[arg-type]
        confidence_threshold=config.confidence_threshold,
        max_inline=config.max_inline,
        enable_suggestions=config.enable_suggestions,
        semaphore_size=config.parallel,
        sarif_paths=config.sarif_paths,
    )
    result = await run_review(
        diff=DiffContext(diff_text=diff_text, head_sha=head_sha),
        summary_text=summary_text,
        agents=agents,
        llm_call=_llm_call,
        dispatch_context=dispatch_ctx,
        provider=provider,
        config=orch_config,
    )

    # 9. Append token cost table to the summary comment (upsert).
    if result.agent_results and result.summary and result.summary.ok:
        _upsert_token_table(result, provider, head_sha, script_dir, summary_text)

    _emit_review_result(result, base_ref=base_ref, head=head_sha)
    return 0 if result.ok else 1


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

    Fail-soft: on any error (missing prompt, LLM failure, parse error) logs a
    WARNING and returns a visible failure notice so the PR comment communicates
    the partial failure rather than silently omitting the summary.
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
        proc: subprocess.CompletedProcess[str] | None = None
        try:
            proc = subprocess.run(
                ["git", "log", "--format=%h %s%n%b", "--max-count=20", f"{base_ref}..HEAD"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            logger.warning("pr-summarizer: git log timed out; proceeding without commit log")
        except Exception as exc:
            logger.warning("pr-summarizer: could not get commit log: %s", exc)
        else:
            if proc.returncode != 0:
                logger.warning(
                    "pr-summarizer: git log exited %d; stderr=%r stdout=%r",
                    proc.returncode, proc.stderr.strip(), proc.stdout.strip(),
                )
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


def _upsert_token_table(
    result: ReviewResult,
    provider: VcsProvider,
    head_sha: str,
    script_dir: Path,
    summary_text: str,
) -> None:
    """Render the token cost table and upsert it into the existing summary comment."""
    from ai_pr_review.agents.dispatch import AgentResult
    from ai_pr_review.pricing import TokenEntry, emit_token_table, load_pricing

    try:
        token_log: list[TokenEntry] = []
        for ar in result.agent_results:
            if isinstance(ar, AgentResult) and ar.token_log is not None:
                tl = ar.token_log
                token_log.append(TokenEntry(
                    agent=ar.name,
                    model=tl.model,
                    input_tokens=tl.input,
                    output_tokens=tl.output,
                    cache_creation_tokens=tl.cache_creation,
                    cache_read_tokens=tl.cache_read,
                ))

        if not token_log:
            return

        pricing_file = str(script_dir / "config" / "model-pricing.json")
        pricing_data = load_pricing(pricing_file)
        table = emit_token_table(token_log, pricing_data)
    except Exception as exc:
        logger.warning(
            "token table: could not generate token table (pricing_file=%r): %s",
            str(script_dir / "config" / "model-pricing.json"), exc, exc_info=True,
        )
        return

    accordion = (
        "<details>\n<summary>Token usage by agent</summary>\n\n"
        + table
        + "\n</details>"
    )
    # post_summary is an upsert — calling it again updates the same comment.
    try:
        new_body = (summary_text.strip() or "## AI Review").rstrip() + "\n\n" + accordion
        provider.post_summary(new_body, head_sha)
    except Exception as exc:
        logger.error(
            "token table: could not post token table to PR comment: %s: %s",
            type(exc).__name__, exc, exc_info=True,
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
    """Handle one /ai-pr-review comment command (Capability C).

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

    from ai_pr_review.feedback.store import make_store

    store = make_store(config)
    entry = build_entry(result, source=source, file=file_path, rule_id=rule_id)
    reply = handle_command(result, entry, store)
    if reply:
        click.echo(reply)


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
        ignore_merge_commits=config.ignore_merge_commits,
        review_target=config.review_target,
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
        "merge_filter_fallback_reason": diff_result.fallback_reason,
        "findings": [],
        "token_log": [],
    }
