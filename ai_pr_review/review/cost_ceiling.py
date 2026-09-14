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

Known estimation bias (#848), disclosed explicitly rather than left implicit
like the approximations above: this estimate is structurally biased *high*,
often by 5-10x against what a run actually spends, for three compounding
reasons, none of which this module corrects for:

  - **Output tokens are the cap, not a prediction.** Every agent's output
    contribution is priced at its full effective cap (``AI_MAX_TOKENS_
    PER_AGENT`` or the agent's own ``max_output_tokens``) -- see
    ``estimate_review_cost``'s docstring. Most calls finish well under that
    cap; the estimate has no way to know how far under before the call is
    actually made, so it prices every agent as if it will use the entire
    budget.
  - **Prompt caching is not modeled at all.** ``llm/anthropic.py`` and
    ``llm/bedrock.py`` mark the shared context block and language-profile
    text ``cache_control: ephemeral``, and a cache *read* is priced far
    below a full input read on every provider that supports it (see
    ``config/model-pricing.json``'s ``cache_read_rate`` vs ``input_rate``
    per model). This estimate prices every agent's shared-context and
    language-profile tokens as full-price input on every call, as if no
    caching were configured -- on a run with several context-enrichment-
    eligible agents sharing the same cached blocks, only the first agent's
    call is realistically a full-price cache write; the rest are cheap
    cache reads this estimate does not discount.
  - **The judge-pass LLM call is omitted from the total.** ``AI_JUDGE_PASS``
    (on by default) makes one additional cheap-model call after the findings
    pipeline (``findings/judge.py``) -- a real cost the pre-flight estimate
    never accounts for, because the judge pass only knows what to score
    after the main roster's findings exist, which is well after this
    estimate runs.

Net effect: a ceiling configured against real historical run costs (rather
than against this estimate's own output) will trip far more often than the
actual spend would justify, since the estimate is not calibrated to be
close to actual cost -- only to never be *low*. Narrowing this gap (partial
caching-aware discounting, a judge-pass cost placeholder) is future work;
until then, treat ``AI_MAX_COST_USD`` as "no more than N times a bad-case
run," not "no more than $N."
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.context.budget import estimate_tokens
from ai_pr_review.pricing import compute_cost_units, format_cost, model_pricing

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
        cost_units = compute_cost_units(
            (input_tokens, rates.input_rate), (output_tokens, rates.output_rate)
        )

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
    cost_units = compute_cost_units(
        (input_tokens, rates.input_rate), (output_tokens, rates.output_rate)
    )
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


def merge_cost_estimates(*parts: CostEstimate | AgentCostEstimate) -> CostEstimate:
    """Combine CostEstimates and/or individual AgentCostEstimates (e.g. the
    main roster's CostEstimate plus one AgentCostEstimate per
    separately-dispatched preflight agent) into one aggregate.

    Accepting bare ``AgentCostEstimate`` entries directly (#848) avoids
    callers having to wrap a single agent's estimate in a throwaway
    ``CostEstimate(per_agent=(x,), total_cost_units=x.estimated_cost_units,
    any_unknown_pricing=x.unknown_pricing)`` just to satisfy this function's
    signature -- ``review/runtime.py``'s pr-summarizer/issue-linker cost
    folding does exactly that.

    Recomputes total_cost_units/any_unknown_pricing from the merged
    per-agent list rather than summing the parts' own totals, so this stays
    correct regardless of how each part was built.
    """
    per_agent: list[AgentCostEstimate] = []
    for part in parts:
        if isinstance(part, AgentCostEstimate):
            per_agent.append(part)
        else:
            per_agent.extend(part.per_agent)
    return CostEstimate(
        per_agent=tuple(per_agent),
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

    Logged at WARNING, not INFO: the shipped default is ``AI_LOG_LEVEL=
    WARNING`` (config.py), so an INFO-level line here would silently never
    appear for any consumer running at the default level, contradicting
    this docstring's own "auditable on every run" claim.
    """
    breakdown = " ".join(
        f"{a.agent}={format_cost(a.estimated_cost_units)}"
        + ("(unpriced)" if a.unknown_pricing else "")
        for a in estimate.per_agent
    )
    logger.warning(
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

    Unpriced-model visibility (#848 follow-up): ``estimate.any_unknown_pricing``
    is computed and logged by ``log_cost_estimate``, but until now was never
    consulted here, at the actual pass/fail decision. An agent using a model
    with no ``config/model-pricing.json`` entry (e.g. any ``openai-compatible``
    deployment, whose model id is user-specified) contributes $0 to
    ``estimate.total_cost_usd`` -- so the ceiling can silently never trip for
    that agent's real spend, and a run that "comes in under budget" is
    indistinguishable in the log from a run where the budget check simply
    couldn't see part of the cost. This does not fail closed (an unpriced
    model may legitimately be a free/internal one) -- it only makes the gap
    loud: whenever a ceiling is configured and any agent is unpriced, a
    distinct warning fires unconditionally, whether or not the priced portion
    ends up exceeding the ceiling.
    """
    if ceiling_usd <= 0:
        return

    if estimate.any_unknown_pricing:
        n_unpriced = sum(1 for a in estimate.per_agent if a.unknown_pricing)
        logger.warning(
            "COST_CEILING_GAP ceiling not enforceable this run: %d agent(s) "
            "unpriced (no entry in config/model-pricing.json) and excluded "
            "from the $%s estimate this ceiling (%s) is checked against -- "
            "their real spend is not reflected in that total either way",
            n_unpriced,
            format_cost(estimate.total_cost_units),
            format_cost(int(round(ceiling_usd * 10000))),
        )

    if estimate.total_cost_usd <= ceiling_usd:
        return

    top = sorted(estimate.per_agent, key=lambda a: a.estimated_cost_units, reverse=True)[:3]
    contributors = ", ".join(
        f"{a.agent} ({format_cost(a.estimated_cost_units)}"
        + (", unpriced -- no cost data, not necessarily free" if a.unknown_pricing else "")
        + ")"
        for a in top
    )
    raise CostCeilingExceeded(
        f"Pre-flight cost estimate ${estimate.total_cost_usd:.4f} exceeds the "
        f"configured ceiling ${ceiling_usd:.2f} (AI_MAX_COST_USD / "
        f"max-cost-usd). Top contributing agents: {contributors}. "
        "This PR's LLM-agent review was skipped before any LLM call was "
        "made. A repo maintainer can raise AI_MAX_COST_USD, switch "
        "AI_REVIEW_MODE to quick, reduce the agent roster via "
        "AI_AGENTS/AI_EXCLUDE_AGENTS, or lower AI_MAX_TOKENS_PER_AGENT in "
        "this repository's workflow/action configuration to allow larger "
        "reviews -- these are repo/workflow-level settings a PR author "
        "cannot change from the PR itself."
    )
