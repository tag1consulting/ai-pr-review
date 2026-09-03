"""Post-review reporting: token table accordion, step summary, and result echo."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from ai_pr_review.orchestrate import ReviewResult
    from ai_pr_review.pricing import TokenEntry, TokenTotals
    from ai_pr_review.review.runtime import ReviewRuntime

logger = logging.getLogger(__name__)


def _build_token_log(
    successes: Sequence[object],
    *,
    effective_max_tokens: int = 0,
    judge_input_tokens: int = 0,
    judge_output_tokens: int = 0,
    judge_cache_creation_tokens: int = 0,
    judge_cache_read_tokens: int = 0,
    judge_model: str = "",
) -> list[TokenEntry]:
    """Assemble the ``TokenEntry`` list shared by every token-usage rendering
    path (#758): the full table, the compact usage line, and the high-usage
    warning all need the exact same rows, so this is the one place that
    builds them from ``AgentResult.token_log`` plus the synthetic
    ``judge-pass`` row.

    ``effective_max_tokens`` is the user-configured cap from
    ``DispatchContext.max_tokens_per_agent`` (i.e. ``AI_MAX_TOKENS_PER_AGENT``).
    When > 0 it overrides the per-agent roster default so the table reflects
    the actual cap sent to the LLM rather than the hard-coded roster value.
    """
    from ai_pr_review.agents.dispatch import AgentResult
    from ai_pr_review.agents.roster import AGENTS
    from ai_pr_review.pricing import TokenEntry

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

    return token_log


@dataclass
class _Prepared:
    token_log: list[TokenEntry]
    pricing_data: list[dict[str, object]]
    context_tokens: int
    profile_tokens: int


def _prepare(
    successes: Sequence[object],
    script_dir: Path,
    *,
    effective_max_tokens: int = 0,
    judge_input_tokens: int = 0,
    judge_output_tokens: int = 0,
    judge_cache_creation_tokens: int = 0,
    judge_cache_read_tokens: int = 0,
    judge_model: str = "",
) -> _Prepared | None:
    """Shared fail-soft setup for every token-usage rendering path: assemble
    the token log, compute the two supplementary-row figures, and load
    pricing data. Returns ``None`` on no-data or a pricing-load failure —
    callers translate that into their own "nothing to show" sentinel
    (``""`` for a rendered string, ``None`` for a ``TokenTotals``).
    """
    from ai_pr_review.agents.dispatch import AgentResult
    from ai_pr_review.pricing import load_pricing

    token_log = _build_token_log(
        successes,
        effective_max_tokens=effective_max_tokens,
        judge_input_tokens=judge_input_tokens,
        judge_output_tokens=judge_output_tokens,
        judge_cache_creation_tokens=judge_cache_creation_tokens,
        judge_cache_read_tokens=judge_cache_read_tokens,
        judge_model=judge_model,
    )
    if not token_log:
        return None

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
        return None

    return _Prepared(
        token_log=token_log,
        pricing_data=pricing_data,
        context_tokens=context_tokens,
        profile_tokens=profile_tokens,
    )


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

    This is the full breakdown: rendered into ``GITHUB_STEP_SUMMARY``
    unconditionally, and into the review comment itself only when
    ``token-usage-display: full`` (#758's default is the compact
    ``build_token_usage_line`` instead — see that function).

    All exceptions are caught and logged as WARNING so token table failure
    never aborts a review.
    """
    prepared = _prepare(
        successes, script_dir,
        effective_max_tokens=effective_max_tokens,
        judge_input_tokens=judge_input_tokens,
        judge_output_tokens=judge_output_tokens,
        judge_cache_creation_tokens=judge_cache_creation_tokens,
        judge_cache_read_tokens=judge_cache_read_tokens,
        judge_model=judge_model,
    )
    if prepared is None:
        return ""

    from ai_pr_review.pricing import emit_token_table

    try:
        table = emit_token_table(
            prepared.token_log,
            prepared.pricing_data,
            context_tokens=prepared.context_tokens,
            profile_tokens=prepared.profile_tokens,
            sarif_elapsed_s=sarif_elapsed_s,
        )
    except Exception as exc:
        logger.warning("token table: could not render table: %s", exc, exc_info=True)
        return ""

    from ai_pr_review.vcs._body import TOKEN_TABLE_OPEN_MARKER

    return TOKEN_TABLE_OPEN_MARKER + "\n\n" + table + "\n</details>"


def build_full_token_table(
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
    """Return the bare markdown token table (no ``<details>`` wrapper), or ""
    on no-data/error.

    Used for the CI job-log echo (#758): GitLab and Bitbucket have no
    ``GITHUB_STEP_SUMMARY`` equivalent, so the full per-agent breakdown is
    always echoed to stderr on every provider regardless of
    ``token-usage-display`` mode (the caller skips the echo entirely under
    ``off``). A job log is read as plain text, not rendered markdown, so the
    ``<details>``/``<summary>`` wrapper would just be noise there.
    """
    prepared = _prepare(
        successes, script_dir,
        effective_max_tokens=effective_max_tokens,
        judge_input_tokens=judge_input_tokens,
        judge_output_tokens=judge_output_tokens,
        judge_cache_creation_tokens=judge_cache_creation_tokens,
        judge_cache_read_tokens=judge_cache_read_tokens,
        judge_model=judge_model,
    )
    if prepared is None:
        return ""

    from ai_pr_review.pricing import emit_token_table

    try:
        return emit_token_table(
            prepared.token_log,
            prepared.pricing_data,
            context_tokens=prepared.context_tokens,
            profile_tokens=prepared.profile_tokens,
            sarif_elapsed_s=sarif_elapsed_s,
        )
    except Exception as exc:
        logger.warning("token table: could not render table: %s", exc, exc_info=True)
        return ""


def compute_token_totals(
    successes: Sequence[object],
    script_dir: Path,
    *,
    effective_max_tokens: int = 0,
    judge_input_tokens: int = 0,
    judge_output_tokens: int = 0,
    judge_cache_creation_tokens: int = 0,
    judge_cache_read_tokens: int = 0,
    judge_model: str = "",
) -> TokenTotals | None:
    """Return the run's ``TokenTotals``, or ``None`` on no-data/error.

    Both ``build_token_usage_line`` and ``build_high_usage_warning`` (#758)
    consume this rather than accumulating their own totals, so the compact
    line, the warning, and the full table (whose Total row is itself backed
    by ``pricing.compute_totals``) can never report different numbers for
    the same run.
    """
    prepared = _prepare(
        successes, script_dir,
        effective_max_tokens=effective_max_tokens,
        judge_input_tokens=judge_input_tokens,
        judge_output_tokens=judge_output_tokens,
        judge_cache_creation_tokens=judge_cache_creation_tokens,
        judge_cache_read_tokens=judge_cache_read_tokens,
        judge_model=judge_model,
    )
    if prepared is None:
        return None

    from ai_pr_review.pricing import compute_totals

    try:
        return compute_totals(prepared.token_log, prepared.pricing_data)
    except Exception as exc:
        logger.warning("token table: could not compute totals: %s", exc, exc_info=True)
        return None


def ci_run_url() -> str:
    """Best-effort URL to the current CI run, for the compact usage line's
    "full breakdown" link (#758). Returns "" when the platform can't be
    determined or its required variable(s) are empty, rather than emit a
    malformed link.

    - **GitHub Actions**: ``$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID``
      — this exact shape is documented on GitHub's own Variables reference.
      Neither ``GITHUB_SERVER_URL`` nor ``GITHUB_RUN_ID`` reaches the
      container by default; both must be passed through in
      ``container-action/action.yml``'s ``docker run`` env list.
    - **GitLab CI/CD**: ``CI_JOB_URL`` — a documented predefined variable
      that is already the job details URL (where the log output lives), so
      no construction is needed. Present natively; the image runs as the
      job container.
    - **Bitbucket Pipelines**: no variable yields a pipeline/step URL
      directly. ``https://bitbucket.org/{BITBUCKET_REPO_FULL_NAME}/pipelines/results/{BITBUCKET_BUILD_NUMBER}``
      is documented only in an Atlassian KB script example, not a
      reference-doc contract — treated as best-effort. Present natively.
    """
    server = os.environ.get("GITHUB_SERVER_URL", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"

    gitlab_job_url = os.environ.get("CI_JOB_URL", "").strip()
    if gitlab_job_url:
        return gitlab_job_url

    bb_repo = os.environ.get("BITBUCKET_REPO_FULL_NAME", "").strip()
    bb_build = os.environ.get("BITBUCKET_BUILD_NUMBER", "").strip()
    if bb_repo and bb_build:
        return f"https://bitbucket.org/{bb_repo}/pipelines/results/{bb_build}"

    return ""


def build_token_usage_line(totals: TokenTotals | None, *, run_url: str = "") -> str:
    """Render the compact one-line usage summary that replaces the full
    table in the review comment by default (#758, ``token-usage-display:
    compact``).

    Returns "" when ``totals`` is ``None`` (no data — mirrors
    ``build_token_table_accordion``'s own no-data sentinel).
    """
    if totals is None:
        return ""

    from ai_pr_review.pricing import format_cost

    cost_str = format_cost(totals.cost_units) + ("+" if totals.any_unknown else "")
    agent_word = "agent" if totals.agent_count == 1 else "agents"
    models_str = ", ".join(totals.models) if totals.models else "unknown model"

    parts = [
        f"Review cost: {cost_str}",
        f"{totals.grand_total:,} tokens",
        f"{totals.agent_count} {agent_word}",
        models_str,
    ]
    line = " · ".join(parts)
    if run_url:
        line += f" · [full breakdown]({run_url})"
    return f"_{line}_"


def build_high_usage_warning(totals: TokenTotals | None, warn_usd: float) -> str:
    """Return a one-line warning when the run's estimated cost crosses
    ``warn_usd``, or "" when it doesn't (or the check is disabled).

    Deliberately never combined into the same string as
    ``build_token_table_accordion``'s output (#758 design decision): a
    warning line embedded inside a collapsed ``<details>`` block would be
    invisible until a reader expanded it, and concatenating it onto the
    accordion string would break Bitbucket's ``_strip_details_wrapper``
    regex, which is anchored on the accordion being the *entire* string.
    Callers append this separately, after whatever ``usage_block`` payload
    the display mode produced.

    When ``totals.any_unknown`` is set (some model in the run has no pricing
    entry), the wording says the figure is a floor rather than asserting a
    precise number — the same run could genuinely cost more than shown, and
    a threshold comparison against an artificially-low total must not
    silently suppress the warning.
    """
    if totals is None or warn_usd <= 0:
        return ""

    warn_units = round(warn_usd * 10000)
    if totals.cost_units <= warn_units:
        return ""

    from ai_pr_review.pricing import format_cost

    cost_str = format_cost(totals.cost_units)
    threshold_str = format_cost(warn_units)

    if totals.any_unknown:
        return (
            f"⚠️ **High token usage:** this review cost at least {cost_str}+ "
            "(some models in this run have no pricing entry, so the true "
            f"cost may be higher) — above the configured {threshold_str} "
            "threshold. See the CI job log for the full per-agent breakdown."
        )
    return (
        f"⚠️ **High token usage:** this review cost {cost_str}, above the "
        f"configured {threshold_str} threshold. See the CI job log for the "
        "full per-agent breakdown."
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
    ``build_token_table_accordion()``. The step summary always shows the
    full table regardless of the review comment's ``token-usage-display``
    mode (#758), so the caller builds this independently of whatever
    ``usage_block``/``usage_warning`` went into the posted review — the two
    calls are already decoupled (each reads the pricing file itself), not a
    single shared build.
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

        # --- Degraded-event trace (silent APPROVE→COMMENT downgrade) ---
        # A degraded post still has ok=True (no error — the COMMENT retry
        # succeeded), so the findings_failed branch above never fires for it.
        # Without this, a rejected APPROVE silently becomes a plain comment
        # that still reads "Approved" with no visible signal anywhere that
        # the PR was never actually approved.
        degraded = (
            result.findings_post is not None
            and result.findings_post.degraded_to_comment
            and result.outcome.event != result.findings_post.event
        )
        if degraded:
            assert result.findings_post is not None
            lines += [
                f"### ⚠️ Review posted as {result.findings_post.event}, "
                f"not {result.outcome.event}",
                "",
                "GitHub rejected the intended review event and ai-pr-review "
                "fell back to a plain comment. **This PR was NOT approved by "
                "ai-pr-review.** See the job log for the underlying HTTP error.",
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


def emit_review_result(
    result: ReviewResult,
    *,
    base_ref: str,
    head: str,
    token_table_full: str = "",
) -> None:
    """Emit a one-line summary to stderr.

    Reports the event actually posted to the VCS (``findings_post.event``)
    rather than only the pre-post decision (``outcome.event``): a provider
    can silently downgrade APPROVE to COMMENT when the approval POST is
    rejected (e.g. GitHub's github.py degrade-to-COMMENT fallback), and this
    line is the only per-run signal a human scanning workflow logs sees, so
    it must not claim an approval that was never actually recorded.

    ``token_table_full`` (#758), when non-empty, is echoed to the CI job log
    right after the summary line. GitLab and Bitbucket have no
    ``GITHUB_STEP_SUMMARY`` equivalent, so this is the only durable place
    those providers' operators can reach the full per-agent breakdown once
    the default review-comment display is the compact usage line rather
    than the full table. Pass "" (the caller's job, e.g. when
    ``token-usage-display: off``) to skip the echo entirely.
    """
    if result.skipped:
        click.echo(f"Review skipped: {result.skip_reason}", err=True)
        return
    n_findings = len(result.findings)
    n_failed = len(result.failed_agents)
    posted = result.findings_post
    event = posted.event if posted is not None else result.outcome.event
    click.echo(
        f"Review complete: {n_findings} findings, "
        f"{n_failed} failed agents, "
        f"event={event}, "
        f"base={base_ref[:7] if base_ref else '?'}..{head[:7] if head else '?'}",
        err=True,
    )
    if token_table_full:
        click.echo("", err=True)
        click.echo("Token usage by agent:", err=True)
        click.echo("", err=True)
        click.echo(token_table_full, err=True)
    # Gated like every other ::warning::/::error:: annotation in this codebase
    # (see emit_post_failure_annotation below, github.py's own degrade
    # annotation): github.py already emits its own ::warning:: for this exact
    # event when running in GitHub Actions, so an ungated echo here would
    # both duplicate that annotation and print raw workflow-command syntax
    # to stderr on local/non-Actions runs.
    if (
        posted is not None
        and posted.degraded_to_comment
        and result.outcome.event != posted.event
        and os.environ.get("GITHUB_ACTIONS") == "true"
    ):
        click.echo(
            f"::warning::ai-pr-review: intended review event was "
            f"{result.outcome.event} but it was posted as {posted.event} "
            "instead — the PR was NOT approved/changes-requested by this run",
            err=True,
        )

    # Canonical-review reuse activity (GitHub only; these fields are always
    # 0/False on GitLab/Bitbucket). Surfacing them is the difference between
    # a maintainer being able to tell from the run log whether reuse engaged
    # at all versus having to reason about it from GitHub's UI after the fact.
    if posted is not None and (
        posted.reused_review or posted.inline_updated or posted.suppressed
        or posted.replies_posted
    ):
        click.echo(
            "Canonical review reuse: "
            f"reused={posted.reused_review}"
            f"{' (skipped, superseded by a newer run)' if posted.skipped else ''}, "
            f"updated={posted.inline_updated}, "
            f"suppressed={posted.suppressed}, "
            f"replies={posted.replies_posted}",
            err=True,
        )

