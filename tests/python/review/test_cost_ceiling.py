"""Tests for ai_pr_review.review.cost_ceiling (#24 -- pre-flight cost ceiling)."""

from __future__ import annotations

import logging

import pytest

from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.review.cost_ceiling import (
    CostCeilingExceeded,
    CostEstimate,
    enforce_cost_ceiling,
    estimate_preflight_agent_cost,
    estimate_review_cost,
    log_cost_estimate,
    merge_cost_estimates,
)

# Rates chosen so the math is easy to hand-verify:
#   input_rate=100_000_000  -> 1 cost unit ($0.0001) per input token
#   output_rate=200_000_000 -> 2 cost units per output token
# (cost_units = (tokens * rate) // 100_000_000, matching pricing._row_cost)
_PRICING = [
    {
        "patterns": ["known-model"],
        "display_name": "Known Model",
        "input_rate": 100_000_000,
        "output_rate": 200_000_000,
    }
]


def _agent(name: str, *, tier: int = 1, context_enrichment_eligible: bool = True) -> AgentSpec:
    return AgentSpec(
        name=name,
        prompt_path=f"prompts/{name}.md",
        tier=tier,
        conditional_trigger=None,
        max_output_tokens=32768,
        full_mode_only=(tier == 2),
        context_enrichment_eligible=context_enrichment_eligible,
    )


class TestEstimateReviewCost:
    def test_known_roster_and_pricing_computes_expected_costs(self) -> None:
        agent_with_context = _agent("with-context", context_enrichment_eligible=True)
        agent_without_context = _agent("without-context", context_enrichment_eligible=False)

        estimate = estimate_review_cost(
            agents=[agent_with_context, agent_without_context],
            diff_text="a" * 1000,  # estimate_tokens -> 275
            shared_context_text="",
            language_profile_text="b" * 400,  # estimate_tokens -> 110
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=1000,
            pricing_data=_PRICING,
        )

        by_name = {a.agent: a for a in estimate.per_agent}

        with_ctx = by_name["with-context"]
        assert with_ctx.estimated_input_tokens == 275 + 110
        assert with_ctx.estimated_output_tokens == 1000
        assert with_ctx.unknown_pricing is False
        assert with_ctx.estimated_cost_units == 2385  # (385*1e8 + 1000*2e8)//1e8

        without_ctx = by_name["without-context"]
        assert without_ctx.estimated_input_tokens == 275
        assert without_ctx.estimated_cost_units == 2275  # (275*1e8 + 1000*2e8)//1e8

        assert estimate.total_cost_units == 2385 + 2275
        assert estimate.total_cost_usd == pytest.approx(0.4660)
        assert estimate.any_unknown_pricing is False

    def test_context_ineligible_agent_excludes_shared_and_profile_tokens(self) -> None:
        agent = _agent("blind-hunter-like", context_enrichment_eligible=False)
        estimate = estimate_review_cost(
            agents=[agent],
            diff_text="x" * 100,
            shared_context_text="y" * 100,
            language_profile_text="z" * 100,
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=500,
            pricing_data=_PRICING,
        )
        # Only the diff contributes -- estimate_tokens("x"*100) = int(100/4*1.1) = 27
        assert estimate.per_agent[0].estimated_input_tokens == 27

    def test_tier2_full_mode_uses_premium_model(self) -> None:
        agent = _agent("architecture-reviewer", tier=2)
        estimate = estimate_review_cost(
            agents=[agent],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="premium-model",
            review_mode="full",
            effective_max_output_tokens=100,
            pricing_data=_PRICING,
        )
        assert estimate.per_agent[0].model == "premium-model"

    def test_tier2_quick_mode_uses_standard_model(self) -> None:
        """Mirrors agents.dispatch's use_premium gate: tier==2 alone is not
        enough -- review_mode must also be "full"."""
        agent = _agent("architecture-reviewer", tier=2)
        estimate = estimate_review_cost(
            agents=[agent],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="premium-model",
            review_mode="quick",
            effective_max_output_tokens=100,
            pricing_data=_PRICING,
        )
        assert estimate.per_agent[0].model == "known-model"

    def test_no_premium_model_configured_falls_back_to_standard(self) -> None:
        agent = _agent("architecture-reviewer", tier=2)
        estimate = estimate_review_cost(
            agents=[agent],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="full",
            effective_max_output_tokens=100,
            pricing_data=_PRICING,
        )
        assert estimate.per_agent[0].model == "known-model"

    def test_unknown_model_is_fail_soft_and_excluded_from_total(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        agent = _agent("code-reviewer")
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.cost_ceiling"):
            estimate = estimate_review_cost(
                agents=[agent],
                diff_text="a" * 1000,
                shared_context_text="",
                language_profile_text="",
                standard_model="totally-unrecognised-model-xyz",
                premium_model="",
                review_mode="quick",
                effective_max_output_tokens=1000,
                pricing_data=_PRICING,
            )
        assert estimate.per_agent[0].unknown_pricing is True
        assert estimate.per_agent[0].estimated_cost_units == 0
        assert estimate.any_unknown_pricing is True
        assert estimate.total_cost_units == 0
        assert "no pricing entry" in caplog.text

    def test_empty_roster_yields_zero_cost(self) -> None:
        estimate = estimate_review_cost(
            agents=[],
            diff_text="a" * 1000,
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=1000,
            pricing_data=_PRICING,
        )
        assert estimate.per_agent == ()
        assert estimate.total_cost_units == 0
        assert estimate.any_unknown_pricing is False


class TestLogCostEstimate:
    def test_emits_cost_estimate_line_with_no_ceiling(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        estimate = estimate_review_cost(
            agents=[_agent("code-reviewer")],
            diff_text="a" * 1000,
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=1000,
            pricing_data=_PRICING,
        )
        with caplog.at_level(logging.INFO, logger="ai_pr_review.review.cost_ceiling"):
            log_cost_estimate(estimate, ceiling_usd=0.0)
        assert "COST_ESTIMATE" in caplog.text
        assert "ceiling=none" in caplog.text
        assert "code-reviewer" in caplog.text

    def test_cost_estimate_line_visible_at_default_warning_level(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The shipped AI_LOG_LEVEL default is WARNING (config.py) -- an
        INFO-level COST_ESTIMATE line would be silently invisible for any
        consumer running at the default level, contradicting this
        function's own "auditable on every run" docstring claim."""
        estimate = estimate_review_cost(
            agents=[_agent("code-reviewer")],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=1000,
            pricing_data=_PRICING,
        )
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.cost_ceiling"):
            log_cost_estimate(estimate, ceiling_usd=0.0)
        assert "COST_ESTIMATE" in caplog.text

    def test_emits_cost_estimate_line_with_ceiling_configured(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        estimate = estimate_review_cost(
            agents=[_agent("code-reviewer")],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=100,
            pricing_data=_PRICING,
        )
        with caplog.at_level(logging.INFO, logger="ai_pr_review.review.cost_ceiling"):
            log_cost_estimate(estimate, ceiling_usd=2.00)
        assert "COST_ESTIMATE" in caplog.text
        assert "ceiling=$2.0000" in caplog.text


class TestEstimatePreflightAgentCost:
    def test_uses_diff_text_only_as_input_proxy(self) -> None:
        estimate = estimate_preflight_agent_cost(
            agent_name="pr-summarizer",
            model="known-model",
            diff_text="a" * 1000,  # estimate_tokens -> 275
            output_tokens=4096,
            pricing_data=_PRICING,
        )
        assert estimate.agent == "pr-summarizer"
        assert estimate.model == "known-model"
        assert estimate.estimated_input_tokens == 275
        assert estimate.estimated_output_tokens == 4096
        # (275*1e8 + 4096*2e8)//1e8
        assert estimate.estimated_cost_units == 275 + 4096 * 2
        assert estimate.unknown_pricing is False

    def test_unknown_model_is_fail_soft(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.cost_ceiling"):
            estimate = estimate_preflight_agent_cost(
                agent_name="issue-linker",
                model="zzz-nonexistent-model-000",
                diff_text="a" * 1000,
                output_tokens=4096,
                pricing_data=_PRICING,
            )
        assert estimate.unknown_pricing is True
        assert estimate.estimated_cost_units == 0
        assert "no pricing entry" in caplog.text


class TestMergeCostEstimates:
    def test_merges_per_agent_entries_and_recomputes_totals(self) -> None:
        main = estimate_review_cost(
            agents=[_agent("code-reviewer")],
            diff_text="a" * 1000,
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=1000,
            pricing_data=_PRICING,
        )
        summarizer_cost = estimate_preflight_agent_cost(
            agent_name="pr-summarizer",
            model="known-model",
            diff_text="a" * 1000,
            output_tokens=4096,
            pricing_data=_PRICING,
        )
        preflight_part = CostEstimate(
            per_agent=(summarizer_cost,),
            total_cost_units=summarizer_cost.estimated_cost_units,
            any_unknown_pricing=summarizer_cost.unknown_pricing,
        )

        merged = merge_cost_estimates(main, preflight_part)

        assert len(merged.per_agent) == 2
        assert {a.agent for a in merged.per_agent} == {"code-reviewer", "pr-summarizer"}
        assert merged.total_cost_units == main.total_cost_units + summarizer_cost.estimated_cost_units

    def test_any_unknown_pricing_true_if_any_part_has_one(self) -> None:
        known = CostEstimate(per_agent=(), total_cost_units=0, any_unknown_pricing=False)
        unknown_cost = estimate_preflight_agent_cost(
            agent_name="issue-linker",
            model="zzz-nonexistent-model-000",
            diff_text="",
            output_tokens=4096,
            pricing_data=_PRICING,
        )
        unknown = CostEstimate(
            per_agent=(unknown_cost,), total_cost_units=0, any_unknown_pricing=True,
        )
        merged = merge_cost_estimates(known, unknown)
        assert merged.any_unknown_pricing is True

    def test_merging_zero_parts_yields_empty_estimate(self) -> None:
        merged = merge_cost_estimates()
        assert merged.per_agent == ()
        assert merged.total_cost_units == 0
        assert merged.any_unknown_pricing is False

    def test_accepts_bare_agent_cost_estimate_without_wrapping(self) -> None:
        """#848: merge_cost_estimates should accept an AgentCostEstimate
        directly, without the caller wrapping it in a throwaway CostEstimate
        first (the shape review/runtime.py used to build by hand)."""
        main = estimate_review_cost(
            agents=[_agent("code-reviewer")],
            diff_text="a" * 1000,
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=1000,
            pricing_data=_PRICING,
        )
        summarizer_cost = estimate_preflight_agent_cost(
            agent_name="pr-summarizer",
            model="known-model",
            diff_text="a" * 1000,
            output_tokens=4096,
            pricing_data=_PRICING,
        )

        merged = merge_cost_estimates(main, summarizer_cost)

        assert len(merged.per_agent) == 2
        assert {a.agent for a in merged.per_agent} == {"code-reviewer", "pr-summarizer"}
        assert merged.total_cost_units == main.total_cost_units + summarizer_cost.estimated_cost_units

    def test_mix_of_cost_estimate_and_agent_cost_estimate_parts(self) -> None:
        summarizer_cost = estimate_preflight_agent_cost(
            agent_name="pr-summarizer",
            model="known-model",
            diff_text="",
            output_tokens=4096,
            pricing_data=_PRICING,
        )
        issue_linker_cost = estimate_preflight_agent_cost(
            agent_name="issue-linker",
            model="known-model",
            diff_text="",
            output_tokens=4096,
            pricing_data=_PRICING,
        )
        empty = CostEstimate(per_agent=(), total_cost_units=0, any_unknown_pricing=False)

        merged = merge_cost_estimates(empty, summarizer_cost, issue_linker_cost)

        assert {a.agent for a in merged.per_agent} == {"pr-summarizer", "issue-linker"}
        assert merged.total_cost_units == (
            summarizer_cost.estimated_cost_units + issue_linker_cost.estimated_cost_units
        )


class TestEnforceCostCeiling:
    def _estimate_with_total_units(self, units: int) -> object:
        # Build a real CostEstimate via estimate_review_cost so the ceiling
        # function is exercised against its actual output shape.
        agent = _agent("code-reviewer")
        # output_tokens=units gives cost_units = units*2 at our rates; solve
        # for the output cap that yields exactly `units` cost_units when
        # input contributes 0 (empty diff/context).
        assert units % 2 == 0
        return estimate_review_cost(
            agents=[agent],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="",
            review_mode="quick",
            effective_max_output_tokens=units // 2,
            pricing_data=_PRICING,
        )

    def test_ceiling_not_configured_is_a_noop(self) -> None:
        estimate = self._estimate_with_total_units(1_000_000)  # $100, way over any sane ceiling
        enforce_cost_ceiling(estimate, ceiling_usd=0.0)  # must not raise

    def test_ceiling_exceeded_raises_with_clear_message(self) -> None:
        estimate = self._estimate_with_total_units(20000)  # $2.00
        with pytest.raises(CostCeilingExceeded) as exc_info:
            enforce_cost_ceiling(estimate, ceiling_usd=1.00)
        message = str(exc_info.value)
        assert "$2.0000" in message
        assert "$1.00" in message
        assert "AI_MAX_COST_USD" in message
        assert "code-reviewer" in message

    def test_ceiling_not_exceeded_does_not_raise(self) -> None:
        estimate = self._estimate_with_total_units(5000)  # $0.50
        enforce_cost_ceiling(estimate, ceiling_usd=1.00)  # must not raise

    def test_ceiling_exactly_equal_to_estimate_is_allowed(self) -> None:
        estimate = self._estimate_with_total_units(10000)  # exactly $1.00
        enforce_cost_ceiling(estimate, ceiling_usd=1.00)  # must not raise

    def test_negative_ceiling_is_a_noop(self) -> None:
        """ReviewConfig clamps negative values to 0 before this is ever
        called, but the function itself should not misbehave if it somehow
        receives one directly."""
        estimate = self._estimate_with_total_units(1_000_000)
        enforce_cost_ceiling(estimate, ceiling_usd=-5.0)  # must not raise

    def test_message_addresses_a_repo_maintainer_not_the_pr_author(self) -> None:
        """#848: the remediation text is posted verbatim to the PR as the
        skip comment (see cli.py/_orchestrate_skip -> post_skip_comment), so
        it must not imply a PR author can set these repo/workflow-level
        env vars themselves."""
        estimate = self._estimate_with_total_units(20000)
        with pytest.raises(CostCeilingExceeded) as exc_info:
            enforce_cost_ceiling(estimate, ceiling_usd=1.00)
        message = str(exc_info.value)
        assert "maintainer" in message
        assert "AI_MAX_COST_USD" in message
        # Still names the knobs a maintainer needs, just not addressed at
        # the PR author.
        assert "AI_REVIEW_MODE" in message
        assert "AI_AGENTS" in message

    def test_unpriced_top_contributor_is_flagged_in_message(self) -> None:
        """#848: a $0 top contributor must be distinguishable as "no pricing
        data" rather than reading as "genuinely free", matching
        log_cost_estimate's existing "(unpriced)" convention."""
        priced_agent = _agent("code-reviewer")  # tier 1 -> standard_model (priced)
        unpriced_agent = _agent("mystery-agent", tier=2)  # tier 2, full mode -> premium_model (unpriced)
        estimate = estimate_review_cost(
            agents=[priced_agent, unpriced_agent],
            diff_text="",
            shared_context_text="",
            language_profile_text="",
            standard_model="known-model",
            premium_model="unrecognised-model-xyz",
            review_mode="full",
            effective_max_output_tokens=5000,  # -> 10000 units for the priced agent ($1.00)
            pricing_data=_PRICING,
        )
        by_name = {a.agent: a for a in estimate.per_agent}
        assert by_name["mystery-agent"].unknown_pricing is True
        assert by_name["mystery-agent"].estimated_cost_units == 0

        with pytest.raises(CostCeilingExceeded) as exc_info:
            enforce_cost_ceiling(estimate, ceiling_usd=0.50)
        message = str(exc_info.value)
        assert "mystery-agent" in message
        assert "unpriced" in message
