"""Tests for ai_pr_review.review.reporting (currently: log_cost_reconciliation, #848)."""

from __future__ import annotations

import logging

import pytest

from ai_pr_review.pricing import TokenTotals
from ai_pr_review.review.reporting import log_cost_reconciliation


def _totals(*, cost_units: int, any_unknown: bool = False) -> TokenTotals:
    return TokenTotals(
        input_tokens=100,
        output_tokens=100,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        grand_total=200,
        cost_units=cost_units,
        any_unknown=any_unknown,
        agent_count=1,
        models=("Sonnet 5",),
    )


class TestLogCostReconciliation:
    def test_logs_estimate_actual_and_delta(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.reporting"):
            log_cost_reconciliation(1.0000, _totals(cost_units=5000))  # $0.50 actual

        assert "COST_RECONCILIATION" in caplog.text
        assert "estimate=$1.0000" in caplog.text
        assert "actual=$0.5000" in caplog.text
        assert "delta=$-0.5000" in caplog.text

    def test_discloses_the_two_asymmetric_exclusions(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#848 follow-up: estimate and actual do not cover the same set of
        LLM calls (estimate includes pr-summarizer/issue-linker, excludes
        the judge pass; actual is the exact opposite) -- this must be
        visible in the log line itself, not just the docstring."""
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.reporting"):
            log_cost_reconciliation(1.0000, _totals(cost_units=5000))

        assert "judge-pass" in caplog.text
        assert "pr-summarizer" in caplog.text
        assert "issue-linker" in caplog.text

    def test_none_estimate_skips_the_line(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.reporting"):
            log_cost_reconciliation(None, _totals(cost_units=5000))

        assert "COST_RECONCILIATION" not in caplog.text

    def test_none_totals_skips_the_line(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.reporting"):
            log_cost_reconciliation(1.0000, None)

        assert "COST_RECONCILIATION" not in caplog.text

    def test_any_unknown_appends_unpriced_note(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.review.reporting"):
            log_cost_reconciliation(1.0000, _totals(cost_units=5000, any_unknown=True))

        assert "unpriced agent" in caplog.text
