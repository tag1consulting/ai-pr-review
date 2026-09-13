"""Pre-flight cost estimation and ceiling enforcement (#24).

Before any agent dispatches, estimate the review's total LLM spend from:

  - each agent's expected *input* tokens, estimated from the diff text plus
    the shared PR-context block and language-profile text via
    ``context.budget.estimate_tokens``'s conservative 4-chars-per-token
    heuristic (the same approximation the context-enrichment budget already
    uses elsewhere in this codebase) -- **not** a real provider tokenizer
    count;
  - each agent's *output* tokens, assumed at the effective per-agent output
    cap (``AI_MAX_TOKENS_PER_AGENT`` when set, else the agent's own
    ``AgentSpec.max_output_tokens``) -- an upper bound, not a prediction of
    what the model will actually generate;
  - per-model rates from ``pricing.py``'s bundled ``config/model-pricing.json``.

This is deliberately an approximation, not an exact invoice preview: real
input token counts depend on provider-specific tokenization (which this
engine has no dependency on), and real output token counts are unknowable
before a model actually responds -- most calls finish well under their cap.
The estimate exists to catch gross cost overruns (a huge diff paired with a
premium model and a full-mode agent roster) before a single dollar is
spent, not to predict a run's cost to the cent.

The estimate covers both the main review-agent roster dispatched via
``agents.dispatch.run_tier`` (``ReviewRuntime.agents`` -- the dominant,
diff-size-scaling cost driver, via ``estimate_review_cost``) and the two
separately-dispatched preflight agents, pr-summarizer and issue-linker, when
they will actually run (via ``estimate_preflight_agent_cost``, a coarser
approximation -- see its docstring for why they need a different one).

Fail-soft (matching this repo's general convention -- see
``findings/judge.py``'s module docstring for the model this follows): a
model with no entry in the pricing file is not an error. Its cost is
excluded from the total (logged as a warning), and the ceiling check simply
does not see that agent's contribution rather than raising or aborting.

Also out of scope, by design: this is a single-pass, single-attempt
estimate. It does not model provider-side retries (rate limits, transient
errors) or the standard/premium model-fallback retry in
``agents/dispatch.py``'s ``_run_single_agent`` (#810) -- a run that retries
could spend more than this estimate says. Modeling that would require
guessing a retry rate, which is not knowable pre-flight either; the ceiling
is a guard against gross overruns from diff size and roster choice, not a
hard cap on worst-case spend.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.context.budget import estimate_tokens
from ai_pr_review.pricing import format_cost, model_pricing

logger = logging.getLogger(__name__)


class CostCeilingExceeded(RuntimeError):
    """Raised when the pre-flight cost estimate exceeds the configured ceiling.

    Callers (cli.py) should catch this, echo the message, and exit non-zero
    before any agent dispatches -- no LLM call has been made when this is
    raised.
    """


@dataclass(frozen=True)
class AgentCostEstimate:
    """One agent's contribution to a review's pre-flight cost estimate."""

    agent: str
    model: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_units: int  # $0.0001 units, same as pricing.format_cost
    unknown_pricing: bool
    """True when *model* has no entry in the pricing data. This agent's
    estimated_cost_units is 0 in that case and it is excluded from the
    ceiling comparison -- see the module docstring's fail-soft note."""


@dataclass(frozen=True)
class CostEstimate:
    """Aggregate pre-flight cost estimate for a review run's agent roster."""

    per_agent: tuple[AgentCostEstimate, ...]
    total_cost_units: int  # $0.0001 units; excludes unknown-pricing agents
    any_unknown_pricing: bool

    @property
    def total_cost_usd(self) -> float:
        return self.total_cost_units / 10000


def estimate_review_cost(
    *,
    agents: Sequence[AgentSpec],
    diff_text: str,
    shared_context_text: str,
    language_profile_text: str,
    standard_model: str,
    premium_model: str,
    review_mode: str,
    effective_max_output_tokens: int,
    pricing_data: list[dict[str, object]],
) -> CostEstimate:
    """Estimate the total pre-dispatch LLM cost for *agents*.

    ``effective_max_output_tokens`` should be the same value dispatch.py's
    ``_run_single_agent`` computes (``AI_MAX_TOKENS_PER_AGENT`` when > 0,
    else falls back per-agent) -- pass ``ReviewConfig.max_tokens_per_agent``.
    A non-context-enrichment-eligible agent (e.g. blind-hunter) does not
    receive the shared context block or language-profile text, matching
    ``agents.dispatch``'s own gating.

    See the module docstring for what this approximates and does not.
    """
    diff_tokens = estimate_tokens(diff_text)
    shared_context_tokens = estimate_tokens(shared_context_text)
    profile_tokens = estimate_tokens(language_profile_text)

    per_agent: list[AgentCostEstimate] = []
    total_units = 0
    any_unknown = False

    for agent in agents:
        use_premium = agent.tier == 2 and review_mode == "full" and bool(premium_model)
        model_id = premium_model if use_premium else standard_model
        if not model_id:
            model_id = standard_model or premium_model

        input_tokens = diff_tokens
        if agent.context_enrichment_eligible:
            input_tokens += shared_context_tokens + profile_tokens

        output_tokens = (
            effective_max_output_tokens
            if effective_max_output_tokens > 0
            else agent.max_output_tokens
        )

        rates = model_pricing(model_id, pricing_data)
        unknown = rates.input_rate == 0 and rates.output_rate == 0
        cost_units = (
            input_tokens * rates.input_rate + output_tokens * rates.output_rate
        ) // 100_000_000

        if unknown:
            any_unknown = True
            logger.warning(
                "cost estimate: no pricing entry for model %r (agent=%s); "
                "this agent's cost is excluded from the estimate and the "
                "ceiling check will not see its contribution",
                model_id, agent.name,
            )
            cost_units = 0
        else:
            total_units += cost_units

        per_agent.append(
            AgentCostEstimate(
                agent=agent.name,
                model=model_id,
                estimated_input_tokens=input_tokens,
                estimated_output_tokens=output_tokens,
                estimated_cost_units=cost_units,
                unknown_pricing=unknown,
            )
        )

    return CostEstimate(
        per_agent=tuple(per_agent),
        total_cost_units=total_units,
        any_unknown_pricing=any_unknown,
    )


def estimate_preflight_agent_cost(
    *,
    agent_name: str,
    model: str,
    diff_text: str,
    output_tokens: int,
    pricing_data: list[dict[str, object]],
) -> AgentCostEstimate:
    """Estimate cost for a separately-dispatched preflight agent (pr-summarizer,
    issue-linker).

    These two agents build their own prompt outside ``agents.dispatch``'s
    generic per-agent path (``review/preflight.py``'s ``run_summarizer`` /
    ``run_issue_linker``): pr-summarizer's real input is the manifest, commit
    log, and diff; issue-linker's is the manifest, commit log, branch name,
    and open-issue list -- neither receives the shared PR-context block or
    language-profile text the generic roster does, and issue-linker's input
    does not scale with diff size at all. Reusing ``estimate_review_cost``'s
    per-agent loop for them would misrepresent both. This uses diff-text size
    alone as a coarse proxy for input tokens (commit log/manifest/open-issue
    list are typically much smaller than the diff for any PR large enough to
    be a cost concern), which overestimates issue-linker specifically for
    large diffs -- an intentional conservative bias, not a safety gap, since
    a run over the ceiling is only informational unless
    ``AI_FAIL_ON_COST_CEILING`` is set. ``output_tokens`` should be 4096,
    matching both agents' actual hardcoded ``LLMRequest.max_tokens`` in
    ``review/preflight.py`` (their ``AgentSpec.max_output_tokens`` in
    ``agents/roster.py`` is not what's actually applied to their calls).
    """
    input_tokens = estimate_tokens(diff_text)
    rates = model_pricing(model, pricing_data)
    unknown = rates.input_rate == 0 and rates.output_rate == 0
    cost_units = (
        input_tokens * rates.input_rate + output_tokens * rates.output_rate
    ) // 100_000_000
    if unknown:
        logger.warning(
            "cost estimate: no pricing entry for model %r (agent=%s); "
            "this agent's cost is excluded from the estimate and the "
            "ceiling check will not see its contribution",
            model, agent_name,
        )
        cost_units = 0
    return AgentCostEstimate(
        agent=agent_name,
        model=model,
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost_units=cost_units,
        unknown_pricing=unknown,
    )


def merge_cost_estimates(*parts: CostEstimate) -> CostEstimate:
    """Combine multiple CostEstimates (e.g. the main roster's plus any
    separately-computed preflight-agent estimates) into one aggregate.

    Recomputes total_cost_units/any_unknown_pricing from the merged
    per-agent list rather than summing the parts' own totals, so this stays
    correct regardless of how each part was built.
    """
    per_agent = tuple(a for part in parts for a in part.per_agent)
    return CostEstimate(
        per_agent=per_agent,
        total_cost_units=sum(a.estimated_cost_units for a in per_agent),
        any_unknown_pricing=any(a.unknown_pricing for a in per_agent),
    )


def log_cost_estimate(estimate: CostEstimate, *, ceiling_usd: float) -> None:
    """Emit the structured ``COST_ESTIMATE`` log line.

    Emitted unconditionally -- whether or not a ceiling is configured, and
    whether or not the estimate exceeds it -- so the estimate is auditable
    on every run. One line per run; per-agent detail is packed into the
    message rather than split into separate log records so a single grep
    for ``COST_ESTIMATE`` finds the whole picture.
    """
    breakdown = " ".join(
        f"{a.agent}={format_cost(a.estimated_cost_units)}"
        + ("(unpriced)" if a.unknown_pricing else "")
        for a in estimate.per_agent
    )
    logger.info(
        "COST_ESTIMATE total=%s ceiling=%s agents=%d any_unknown_pricing=%s %s",
        format_cost(estimate.total_cost_units),
        format_cost(int(round(ceiling_usd * 10000))) if ceiling_usd > 0 else "none",
        len(estimate.per_agent),
        estimate.any_unknown_pricing,
        breakdown,
    )


def enforce_cost_ceiling(estimate: CostEstimate, *, ceiling_usd: float) -> None:
    """Raise ``CostCeilingExceeded`` if *estimate* exceeds *ceiling_usd*.

    A ceiling of 0 (or less -- ``ReviewConfig`` already clamps negative
    values to 0) means "no ceiling configured"; this is a no-op in that
    case, matching ``AI_TOKEN_USAGE_WARN_USD``'s existing "0 disables"
    convention in this codebase. Equality (estimate == ceiling) does not
    exceed the ceiling and is allowed through, per the issue's acceptance
    criteria.
    """
    if ceiling_usd <= 0:
        return
    if estimate.total_cost_usd <= ceiling_usd:
        return

    top = sorted(estimate.per_agent, key=lambda a: a.estimated_cost_units, reverse=True)[:3]
    contributors = ", ".join(
        f"{a.agent} ({format_cost(a.estimated_cost_units)})" for a in top
    )
    raise CostCeilingExceeded(
        f"Pre-flight cost estimate ${estimate.total_cost_usd:.4f} exceeds the "
        f"configured ceiling ${ceiling_usd:.2f} (AI_MAX_COST_USD / "
        f"max-cost-usd). Top contributing agents: {contributors}. "
        "Raise AI_MAX_COST_USD, switch AI_REVIEW_MODE to quick, reduce the "
        "agent roster via AI_AGENTS/AI_EXCLUDE_AGENTS, or lower "
        "AI_MAX_TOKENS_PER_AGENT to proceed."
    )
