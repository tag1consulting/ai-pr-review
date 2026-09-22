"""Tests for ai_pr_review.llm._config.

resolve_temperature() gates which models accept a non-default temperature.
Getting this wrong silently 400s every request on a model swap (the model
rejects the sampling param), so each model family's expected behavior gets
a locked-in regression case here.
"""

from __future__ import annotations

from ai_pr_review.llm._config import resolve_effort, resolve_temperature


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


def test_resolve_temperature_rejected_for_opus_5() -> None:
    """Opus 5 was live-verified (2026-09-03) to reject temperature identically
    to Opus 4.8, ahead of its default-model bump."""
    assert resolve_temperature(0.3, "claude-opus-5") is None


def test_resolve_temperature_rejected_for_opus_5_5() -> None:
    """Opus 5.5 removes sampling params per Anthropic's docs, same as Opus 5/4.8/4.7.
    Not yet live-verified -- see the module docstring note in _config.py."""
    assert resolve_temperature(0.3, "claude-opus-5-5") is None


def test_resolve_temperature_rejected_for_bedrock_opus_5_5() -> None:
    """Regression lock: a provider-prefixed Opus 5.5 id must also be rejected."""
    assert resolve_temperature(0.3, "global.anthropic.claude-opus-5-5") is None


def test_resolve_temperature_rejected_for_bedrock_opus_5() -> None:
    """Regression lock: a provider-prefixed Opus 5 id (no further suffix) still matches."""
    assert resolve_temperature(0.3, "us.anthropic.claude-opus-5") is None


def test_resolve_temperature_accepted_for_hypothetical_opus_5_9() -> None:
    """Explicit-match regression lock: a hypothetical claude-opus-5-9 is NOT
    silently treated as the known-verified opus-5/opus-5-5 family just because
    it shares the "opus-5" substring."""
    assert resolve_temperature(0.3, "claude-opus-5-9") == 0.3


def test_resolve_temperature_rejected_for_dated_opus_5_snapshot() -> None:
    """Regression lock: a dated Opus 5 snapshot id (Anthropic's own model-naming
    convention, e.g. "-20260915") must still be recognized as opus-5 family --
    a naive anchor that rejects any digit suffix (not just a single sibling-
    version digit) would silently stop stripping temperature for it."""
    assert resolve_temperature(0.3, "claude-opus-5-20260915") is None


def test_resolve_temperature_accepted_for_sonnet_4_6() -> None:
    """Regression lock: Sonnet 4.6 still accepts a non-default temperature."""
    assert resolve_temperature(0.3, "claude-sonnet-4-6") == 0.3


def test_resolve_temperature_clamps_to_max() -> None:
    """Values above 2.0 are clamped for models that accept temperature."""
    assert resolve_temperature(3.5, "claude-sonnet-4-6") == 2.0


def test_resolve_effort_low_for_sonnet_5() -> None:
    """Sonnet 5's adaptive thinking can exhaust max_tokens before any text is
    produced (issue #592); capping effort at "low" bounds thinking without
    disabling it outright.
    """
    assert resolve_effort("claude-sonnet-5") == "low"


def test_resolve_effort_low_for_bedrock_sonnet_5() -> None:
    """Regression lock: the bedrock-proxy model id must also get the cap."""
    assert resolve_effort("us.anthropic.claude-sonnet-5") == "low"


def test_resolve_effort_omitted_for_opus_4_8() -> None:
    """Opus 4.8 has thinking off by default (requires explicit opt-in), so it
    doesn't have the #592 failure mode and must not receive output_config.
    """
    assert resolve_effort("claude-opus-4-8") is None


def test_resolve_effort_omitted_for_opus_4_7() -> None:
    """Regression lock: Opus 4.7 also has thinking off by default."""
    assert resolve_effort("claude-opus-4-7") is None


def test_resolve_effort_low_for_opus_5() -> None:
    """Unlike Opus 4.7/4.8, Opus 5 has adaptive thinking ON by default -- live
    verified 2026-09-03 against tests/canary/stress_diff.txt: 206s/11,433
    thinking tokens uncapped vs. 67s/2,464 capped at effort="low", the same
    mitigation Sonnet 5 needed for issue #592. Ahead of the default-model
    bump, so this must not regress back to None."""
    assert resolve_effort("claude-opus-5") == "low"


def test_resolve_effort_omitted_for_sonnet_4_6() -> None:
    """Sonnet 4.6 predates output_config.effort; sending it risks a 400."""
    assert resolve_effort("claude-sonnet-4-6") is None


def test_resolve_effort_low_for_opus_5_5() -> None:
    """claude-opus-5-5 cannot disable thinking at all and defaults to effort
    "medium" per Anthropic's docs; "low" is a valid, lower setting. NOT yet
    live-verified against the 180s client timeout -- see the module docstring
    note in _config.py and the merge gate in this repo's CLAUDE.md."""
    assert resolve_effort("claude-opus-5-5") == "low"


def test_resolve_effort_low_for_bedrock_opus_5_5() -> None:
    """Regression lock: a provider-prefixed Opus 5.5 id must also get the cap."""
    assert resolve_effort("global.anthropic.claude-opus-5-5") == "low"


def test_resolve_effort_omitted_for_hypothetical_opus_5_9() -> None:
    """Explicit-match regression lock: a hypothetical claude-opus-5-9 does not
    inherit the opus-5/opus-5-5 effort cap just because it shares the
    "opus-5" substring."""
    assert resolve_effort("claude-opus-5-9") is None


def test_resolve_effort_low_for_dated_opus_5_snapshot() -> None:
    """Regression lock: a dated Opus 5 snapshot id must still get the effort
    cap -- same reasoning as test_resolve_temperature_rejected_for_dated_opus_5_snapshot."""
    assert resolve_effort("claude-opus-5-20260915") == "low"
