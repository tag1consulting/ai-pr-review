"""End-to-end review orchestrator: compute → dispatch → post.

Wires the Epic 1 compute layer, Epic 2 agent dispatch + outcome classifier +
watermark, and the VCS provider posting layer into a single function.

Designed to be unit-testable: callers inject the LLM call, the diff text,
the agent roster, and a VcsProvider. The CLI subcommand `review` is a
thin wrapper that builds these from environment.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

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
    skipped: bool = False
    skip_reason: str = ""

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
) -> ReviewResult:
    """End-to-end review: compute is upstream; this runs dispatch + post.

    Pre-conditions (caller-enforced):
    - diff.diff_text is the unified diff for the review window
    - diff.head_sha is a valid hex SHA
    - summary_text is the pr-summarizer output (may be empty)
    - agents is the gated/filtered roster (S2 + S4)
    - llm_call is bound to the configured provider/model

    Steps:
    1. If skip_reason set, post skip comment and return.
    2. Run agent tier; collect AgentResult + FailedAgent.
    3. Extract findings from each agent's output, then merge + suppress.
    4. Classify the outcome.
    5. Post summary, then post findings, then resolve stale (in that order).
    """
    cfg = config or OrchestrationConfig()

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

    # Phase 1: dispatch agents
    successes: list[AgentResult]
    failures: list[FailedAgent]
    if agents:
        successes, failures = await run_tier(
            list(agents), llm_call, dispatch_context, cfg.semaphore_size
        )
    else:
        successes, failures = [], []

    # Phase 2: extract + merge + suppress
    raw_findings: list[Finding] = []
    for s in successes:
        raw_findings.extend(
            extract_findings(s.output, agent_name=s.name, truncated=s.truncated)
        )
    merged = merge_findings(raw_findings, confidence_threshold=cfg.confidence_threshold)
    kept, _suppressed_count = apply_suppressions(merged, list(cfg.suppression_rules))

    # Phase 3: outcome classification.
    # classify_review_outcome's Protocol declares severity: str; Finding's
    # Literal["Critical", ...] is technically a subtype but Protocol attrs
    # are invariant under mypy. Wrap to satisfy the type checker.
    outcome = classify_review_outcome(
        [_AsFindingLike(f) for f in kept],
        [f.name for f in failures],
        cfg.mode,
    )

    # Phase 4: post summary then findings (AC5 ordering)
    try:
        summary_result = provider.post_summary(
            summary_text or "## AI Review", diff.head_sha
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

    # Phase 5: stale cleanup runs only after a successful post.
    stale_result: StaleResult | None = None
    if summary_result.ok and (findings_result is None or findings_result.ok):
        try:
            stale_result = provider.resolve_stale()
        except RetryExhaustedError as exc:
            # Partial progress (threads resolved before the error) is lost here
            # because providers raise before returning a partial StaleResult.
            # Callers see the error in StaleResult.errors; the review itself
            # is still considered successful (summary + findings were posted).
            stale_result = StaleResult(errors=(str(exc),))

    return ReviewResult(
        findings=kept,
        outcome=outcome,
        failed_agents=failures,
        summary=summary_result,
        findings_post=findings_result,
        stale=stale_result,
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
