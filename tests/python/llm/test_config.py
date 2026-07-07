"""Tests for ai_pr_review.llm._config.

resolve_temperature() gates which models accept a non-default temperature.
Getting this wrong silently 400s every request on a model swap (the model
rejects the sampling param), so each model family's expected behavior gets
a locked-in regression case here.
"""

from __future__ import annotations

from ai_pr_review.llm._config import resolve_temperature


def test_resolve_temperature_rejected_for_sonnet_5() -> None:
    """Sonnet 5 rejects non-default temperature/top_p/top_k; must be omitted."""
    assert resolve_temperature(0.3, "claude-sonnet-5") is None


def test_resolve_temperature_rejected_for_bedrock_sonnet_5() -> None:
    """Regression lock: the bedrock-proxy standard default must also be rejected."""
    assert resolve_temperature(0.3, "us.anthropic.claude-sonnet-5") is None


def test_resolve_temperature_rejected_for_opus_4_8() -> None:
    """Regression lock: Opus 4.8 already rejects temperature."""
    assert resolve_temperature(0.3, "claude-opus-4-8") is None


def test_resolve_temperature_rejected_for_opus_4_7() -> None:
    """Regression lock: Opus 4.7 already rejects temperature."""
    assert resolve_temperature(0.3, "claude-opus-4-7") is None


def test_resolve_temperature_accepted_for_sonnet_4_6() -> None:
    """Regression lock: Sonnet 4.6 still accepts a non-default temperature."""
    assert resolve_temperature(0.3, "claude-sonnet-4-6") == 0.3


def test_resolve_temperature_clamps_to_max() -> None:
    """Values above 2.0 are clamped for models that accept temperature."""
    assert resolve_temperature(3.5, "claude-sonnet-4-6") == 2.0
