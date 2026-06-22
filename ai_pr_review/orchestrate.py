"""End-to-end review orchestrator: dispatch agents, merge findings, post.

All inputs are pre-built by the caller (build_review_runtime). Designed to
be unit-testable: callers inject the LLM call, the diff text, the agent
roster, and a VcsProvider.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ai_pr_review.agents.dispatch import (
    AgentResult,
    DispatchContext,
    FailedAgent,
    LLMCall,
    run_tier,
)
from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.findings.extract import extract_findings
from ai_pr_review.findings.merge import merge_findings
from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.suppress import SuppressionRule, apply_suppressions
from ai_pr_review.review.outcome import (
    ReviewMode,
    ReviewOutcome,
    classify_review_outcome,
)
from ai_pr_review.vcs.http import RetryExhaustedError
from ai_pr_review.vcs.protocol import (
    DiffContext,
    FindingsResult,
    StaleResult,
    SummaryResult,
    VcsProvider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewResult:
    """Aggregate outcome of a single end-to-end review run."""

    findings: list[Finding]
    outcome: ReviewOutcome
    failed_agents: list[FailedAgent]
    summary: SummaryResult | None
    findings_post: FindingsResult | None
    stale: StaleResult | None
    agent_results: list[AgentResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    sarif_elapsed_s: float | None = None

    @property
    def ok(self) -> bool:
        if self.skipped:
            return True
        if self.summary is not None and not self.summary.ok:
            return False
        return not (self.findings_post is not None and not self.findings_post.ok)


@dataclass(frozen=True)
class OrchestrationConfig:
    """Knobs the orchestrator needs in addition to provider + agents."""

    mode: ReviewMode = "full"
    confidence_threshold: int = 75
    max_inline: int = 25
    enable_suggestions: bool = True
    semaphore_size: int = 4
    suppression_rules: tuple[SuppressionRule, ...] = ()
    # Pre-computed findings to inject alongside LLM findings (e.g. from
    # native static analyzers and SARIF, assembled by the caller).
    # Typed as tuple[object,...] to avoid a circular import at module level.
    extra_findings: tuple[object, ...] = ()
    # Controls how out-of-diff native-analyzer findings are handled.
    # Mirrors ReviewConfig.analyzer_diff_scope ("cap" / "drop" / "off").
    analyzer_diff_scope: str = "cap"
    # Judge pass: one cheap-model LLM call after Phase 2.5 to down-rank weak
    # single-source findings. On by default (Story 7-3, #360 remainder).
    enable_judge_pass: bool = True
    judge_model: str = ""
    judge_prompt_path: Path | None = None


TokenTableRenderer = Callable[[Sequence[AgentResult], float | None], str]


async def run_review(
    *,
    diff: DiffContext,
    summary_text: str,
    agents: Sequence[AgentSpec],
    llm_call: LLMCall,
    dispatch_context: DispatchContext,
    provider: VcsProvider,
    config: OrchestrationConfig | None = None,
    skip_reason: str = "",
    token_table_renderer: TokenTableRenderer | None = None,
) -> ReviewResult:
    """End-to-end review: compute is upstream; this runs dispatch + post.

    Pre-conditions (caller-enforced):
    - diff.diff_text is the unified diff for the review window
    - diff.head_sha is a valid hex SHA
    - summary_text is the pr-summarizer output (may be empty)
    - agents is the gated/filtered roster (mode-filtered, then gate-filtered by build_review_runtime)
    - llm_call is bound to the configured provider/model

    Steps:
    1. If skip_reason set, post skip comment and return.
    2. Run agent tier; collect AgentResult + FailedAgent.
    3. Extract findings from each agent's output, then merge + suppress.
    4. Classify the outcome.
    5. Post summary, then post findings, then resolve stale (in that order).
    """
    cfg = config or OrchestrationConfig()

    # Reset the per-run symbol-lookup cache. In a long-lived process
    # (container reuse, future server mode) the cache would otherwise
    # accumulate state across reviews.
    if dispatch_context.enable_context_enrichment:
        from ai_pr_review.context.symbols import _reset_cache
        _reset_cache()

    if skip_reason:
        skip_result = provider.post_skip_comment(skip_reason)
        return ReviewResult(
            findings=[],
            outcome=classify_review_outcome(
                [], [], cfg.mode
            ),
            failed_agents=[],
            summary=skip_result,
            findings_post=None,
            stale=None,
            skipped=True,
            skip_reason=skip_reason,
        )

    # Note: feedback addendum is wired by the caller via
    # DispatchContext.feedback_addendum (see review/runtime.py).
    # The orchestrator does not duplicate that injection.

    # Phase 1: dispatch agents
    successes: list[AgentResult]
    failures: list[FailedAgent]
    if agents:
        successes, failures = await run_tier(
            list(agents), llm_call, dispatch_context, cfg.semaphore_size
        )
    else:
        successes, failures = [], []

    # Phase 1.5: inject pre-computed findings (SARIF, native analyzers) assembled by caller.
    raw_findings: list[Finding] = []
    sarif_elapsed_s: float | None = None
    if cfg.extra_findings:
        from ai_pr_review.findings.models import Finding as _Finding
        injected = 0
        for f in cfg.extra_findings:
            if isinstance(f, _Finding):
                raw_findings.append(f)
                injected += 1
            else:
                logger.warning(
                    "orchestrate: dropped extra_finding of unexpected type %s",
                    type(f).__name__,
                )
        logger.info("analyzers: injected %d pre-computed finding(s)", injected)

    # Phase 2: extract + merge + suppress
    for s in successes:
        raw_findings.extend(
            extract_findings(s.output, agent_name=s.name, truncated=s.truncated)
        )
    merged = merge_findings(raw_findings, confidence_threshold=cfg.confidence_threshold)
    kept, _suppressed_count = apply_suppressions(merged, list(cfg.suppression_rules))

    # Phase 2.5: diff-scope and rollup for native-analyzer findings.
    # Out-of-diff analyzer findings are capped to Low (or dropped) so that
    # whole-file linting on a small diff can never dominate the review or
    # trigger CHANGES_REQUESTED.  Repeated-rule rollup prevents the same
    # analyzer rule from producing dozens of identical body entries.
    if cfg.analyzer_diff_scope != "off":
        from ai_pr_review.findings.scope import apply_diff_scope, rollup_repeated_findings
        try:
            kept = apply_diff_scope(kept, diff.diff_text, mode=cfg.analyzer_diff_scope)
        except Exception as exc:
            logger.warning(
                "apply_diff_scope failed (head_sha=%s); proceeding with unscoped findings: %s",
                diff.head_sha, exc, exc_info=True,
            )
        try:
            kept = rollup_repeated_findings(kept)
        except Exception as exc:
            logger.warning(
                "rollup_repeated_findings failed (head_sha=%s); proceeding without rollup: %s",
                diff.head_sha, exc, exc_info=True,
            )

    # Phase 2.75: judge pass — down-rank weak single-source findings.
    # Runs after merge/suppress/scope/rollup; before outcome classification.
    # The judge uses a cheap model and sends only the candidate list (no diff).
    # Fail-soft: any error returns kept unchanged. Corroborated findings exempt.
    if cfg.enable_judge_pass and kept and cfg.judge_model and cfg.judge_prompt_path:
        from ai_pr_review.findings.judge import judge_findings
        try:
            kept = await judge_findings(
                kept,
                llm_call=llm_call,
                model=cfg.judge_model,
                prompt_path=cfg.judge_prompt_path,
            )
        except Exception as exc:
            logger.warning(
                "judge pass raised unexpectedly (fail-soft): %s", exc, exc_info=True
            )

    # Phase 3: outcome classification.
    # classify_review_outcome's Protocol declares severity: str; Finding's
    # Literal["Critical", ...] is technically a subtype but Protocol attrs
    # are invariant under mypy. Wrap to satisfy the type checker.
    outcome = classify_review_outcome(
        [_AsFindingLike(f) for f in kept],
        [f.name for f in failures],
        cfg.mode,
    )

    # Phase 3.5: render token table (fail-soft; "" disables insertion).
    token_table = ""
    if token_table_renderer is not None:
        try:
            token_table = token_table_renderer(successes, sarif_elapsed_s)
        except Exception as exc:
            logger.warning(
                "token table renderer raised (head_sha=%s): %s",
                diff.head_sha, exc, exc_info=True,
            )

    # Phase 4: post summary then findings (AC5 ordering).
    #
    # When summary_text is empty (incremental run — summarizer was skipped),
    # advance the SHA watermark in the existing summary comment rather than
    # overwriting its body with the fallback "## AI Review" placeholder.
    # On the first review (no existing comment) summary_text will always be
    # non-empty, so advance_sha_watermark is never called when there is nothing
    # to advance.
    # On incremental runs (summary_text empty) we do NOT advance the watermark
    # here. If we did, and post_findings then failed, the durable summary
    # comment's sha= marker would have moved forward while the findings for
    # the in-flight commits were never posted — the next incremental run would
    # silently skip those commits. Defer the advance to after post_findings.ok
    # so the run is all-or-nothing, mirroring the resolve_stale gating at
    # Phase 5 (#493).
    try:
        if not summary_text:
            # Incremental run — don't overwrite the existing summary body.
            # Treat as a successful no-op so findings can still be posted.
            summary_result = SummaryResult(
                comment_id=None, created=False, updated=False
            )
        else:
            summary_result = provider.post_summary(
                summary_text, diff.head_sha
            )
    except RetryExhaustedError as exc:
        err = f"post_summary retry exhausted: {exc}"
        logger.error(err)
        summary_result = SummaryResult(
            comment_id=None, created=False, updated=False, error=err
        )
    findings_result: FindingsResult | None = None
    if summary_result.ok:
        try:
            findings_result = provider.post_findings(
                kept,
                diff,
                event=outcome.event,
                failed_agents=[f.name for f in failures],
                token_table=token_table,
                max_inline=cfg.max_inline,
                enable_suggestions=cfg.enable_suggestions,
            )
        except RetryExhaustedError as exc:
            err = f"post_findings retry exhausted: {exc}"
            logger.error(err)
            findings_result = FindingsResult(
                review_id=None,
                inline_posted=0,
                body_findings=len(kept),
                event=outcome.event,
                error=err,
            )
    else:
        logger.error(
            "post_summary failed (%s); skipping findings post", summary_result.error
        )
        findings_result = FindingsResult(
            review_id=None,
            inline_posted=0,
            body_findings=len(kept),
            event=outcome.event,
            error=f"skipped: summary post failed ({summary_result.error})",
        )

    # Incremental-run watermark advance (#493): only after post_findings
    # succeeded end-to-end. On failure, the watermark stays at the prior SHA
    # so the next run re-diffs from the right baseline and re-attempts the
    # missed findings.
    if not summary_text and findings_result is not None and findings_result.ok:
        try:
            advanced = provider.advance_sha_watermark(diff.head_sha)
            if not advanced:
                logger.warning(
                    "orchestrate: advance_sha_watermark returned False "
                    "(no existing summary comment or no-op replacement) for head_sha=%s; "
                    "next incremental run may re-diff from an older baseline",
                    diff.head_sha,
                )
        except RetryExhaustedError as exc:
            logger.error(
                "advance_sha_watermark retry exhausted: %s; "
                "next incremental run will re-diff from the prior baseline", exc,
            )

    # Phase 5: stale cleanup runs only after a successful post.
    stale_result: StaleResult | None = None
    findings_failed = findings_result is not None and not findings_result.ok
    if summary_result.ok and not findings_failed:
        try:
            stale_result = provider.resolve_stale()
        except RetryExhaustedError as exc:
            # Partial progress (threads resolved before the error) is lost here
            # because providers raise before returning a partial StaleResult.
            # Callers see the error in StaleResult.errors; the review itself
            # is still considered successful (summary + findings were posted).
            stale_result = StaleResult(errors=(str(exc),))
    elif findings_failed:
        assert findings_result is not None
        logger.warning(
            "post_findings failed (%s); skipping stale cleanup", findings_result.error
        )

    return ReviewResult(
        findings=kept,
        outcome=outcome,
        failed_agents=failures,
        summary=summary_result,
        findings_post=findings_result,
        stale=stale_result,
        agent_results=successes,
        sarif_elapsed_s=sarif_elapsed_s,
    )


class _AsFindingLike:
    """Adapter so a `Finding` can be passed to outcome's `_FindingLike` Protocol.

    Outcome's Protocol declares `severity: str`; Finding's
    `Literal["Critical","High","Medium","Low"]` is a subtype but mypy treats
    Protocol attrs as invariant. This adapter is a one-line bridge.
    """

    __slots__ = ("severity",)

    def __init__(self, f: Finding) -> None:
        self.severity: str = f.severity
