"""Post-review reporting: token table accordion, step summary, and result echo."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ai_pr_review.orchestrate import ReviewResult
    from ai_pr_review.review.runtime import ReviewRuntime

logger = logging.getLogger(__name__)


def build_token_table_accordion(
    successes: Sequence[object],
    sarif_elapsed_s: float | None,
    script_dir: Path,
    *,
    effective_max_tokens: int = 0,
    judge_input_tokens: int = 0,
    judge_output_tokens: int = 0,
    judge_cache_creation_tokens: int = 0,
    judge_cache_read_tokens: int = 0,
    judge_model: str = "",
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

    if judge_model and (judge_input_tokens > 0 or judge_output_tokens > 0):
        token_log.append(TokenEntry(
            agent="judge-pass",
            model=judge_model,
            input_tokens=judge_input_tokens,
            output_tokens=judge_output_tokens,
            cache_creation_tokens=judge_cache_creation_tokens,
            cache_read_tokens=judge_cache_read_tokens,
        ))

    if not token_log:
        return ""

    # All enriched agents receive the same context block; take the max (which
    # equals the single non-zero value) rather than summing to avoid double-counting.
    context_tokens = max(
        (ar.context_tokens_used for ar in successes if isinstance(ar, AgentResult)),
        default=0,
    )
    # Profile routing gives each agent a different section subset; take the max
    # as a representative figure (the largest profile slice sent to any agent).
    profile_tokens = max(
        (ar.profile_tokens_used for ar in successes if isinstance(ar, AgentResult)),
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
            profile_tokens=profile_tokens,
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


def write_step_summary(
    result: ReviewResult,
    runtime: ReviewRuntime,
    summary_text: str,
    token_table_md: str = "",
) -> None:
    """Write a concise run summary to GITHUB_STEP_SUMMARY when available.

    Mirrors the bash engine's Phase 4 step-summary block. Fail-soft: any
    error is logged as WARNING and the review result is unaffected.

    ``token_table_md`` should be the pre-built accordion string from
    ``build_token_table_accordion()`` so the step summary matches the PR
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

        # --- Durable fallback trace when the post itself failed (#588) ---
        # post_summary/post_findings can fail (e.g. a 401) after every agent
        # has already run and findings have been computed. The step summary
        # is written unconditionally regardless of post outcome, so it is
        # the one durable place left to record findings that never reached
        # the PR. No API call is involved, so this path can never itself 401.
        summary_failed = result.summary is not None and not result.summary.ok
        findings_failed = result.findings_post is not None and not result.findings_post.ok
        if summary_failed or findings_failed:
            from ai_pr_review.vcs._body import format_body_finding, join_findings

            lines += [
                "### ⚠️ Posting failed: findings below were computed but NOT posted to the PR",
                "",
            ]
            if result.summary is not None and not result.summary.ok and result.summary.error:
                lines += [f"**Summary post error:** {result.summary.error}", ""]
            if (
                result.findings_post is not None
                and not result.findings_post.ok
                and result.findings_post.error
            ):
                lines += [f"**Findings post error:** {result.findings_post.error}", ""]
            if result.findings:
                lines += [
                    join_findings(format_body_finding(f) for f in result.findings),
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


def emit_post_failure_annotation(result: ReviewResult) -> None:
    """Emit a GitHub Actions ``::error::`` annotation when posting failed (#588).

    ``write_step_summary`` (#617) gives the failure a durable trace, but that
    trace lives in ``GITHUB_STEP_SUMMARY``, which most PR reviewers never
    open — it is only visible from the Actions run UI. A workflow-command
    annotation is written straight to the step's stdout, which GitHub Actions
    parses automatically (no API call, so this path can never itself 401) and
    surfaces on the PR's own Checks tab, so a reviewer looking at the PR sees
    a concrete "the review couldn't post" signal rather than silence or a
    bare red X indistinguishable from any other job failure.

    Only fires when ``GITHUB_ACTIONS`` is set (avoids polluting local/test
    runs, matching the guard already used in ``vcs/github.py``).

    Deliberately does NOT key off ``result.ok``: ``ReviewResult.ok`` returns
    True unconditionally when ``result.skipped`` is set (see
    ``ReviewResult.ok`` in ``orchestrate.py``), even if the skip-comment post
    itself failed (``provider.post_skip_comment`` can 401 exactly like
    ``post_summary``/``post_findings`` can). Checking ``result.ok`` directly
    would make this function permanently silent on the skip path -- the same
    silent-failure class #588 exists to fix. Instead this checks
    ``result.summary``/``result.findings_post`` for a populated ``.error``
    directly, which is accurate on both the normal review path and the skip
    path. A crash elsewhere in the pipeline raises before a ``ReviewResult``
    exists at all (caught separately in ``cli.py``'s top-level handler), so
    reaching here with either error field set already means "the review (or
    skip) ran fine but could not post."

    The message deliberately does not interpolate the raw post error: that
    detail already lives in the step summary, and keeping it out of the
    annotation avoids any risk of leaking credential/secret fragments that
    might appear in a provider error string.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    summary_failed = result.summary is not None and not result.summary.ok
    findings_failed = result.findings_post is not None and not result.findings_post.ok
    if not (summary_failed or findings_failed):
        return

    n_findings = len(result.findings)
    click.echo(
        "::error::ai-pr-review: the review ran but could not post "
        f"{n_findings} finding(s) to this PR (see the job's step summary "
        "for the error detail and the computed findings).",
        err=True,
    )


def emit_review_result(result: ReviewResult, *, base_ref: str, head: str) -> None:
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

