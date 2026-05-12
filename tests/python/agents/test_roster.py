"""Tests for ai_pr_review.agents.roster."""

from __future__ import annotations

import os

import pytest

from ai_pr_review.agents.roster import AGENTS, AgentSpec, get_agent

# ---------------------------------------------------------------------------
# AgentSpec validation
# ---------------------------------------------------------------------------

def test_agentspec_valid() -> None:
    spec = AgentSpec(
        name="my-agent",
        prompt_path="prompts/my-agent.md",
        tier=1,
        conditional_trigger=None,
        max_output_tokens=8192,
        full_mode_only=False,
        context_enrichment_eligible=True,
    )
    assert spec.name == "my-agent"
    assert spec.tier == 1


def test_agentspec_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        AgentSpec(
            name="",
            prompt_path="prompts/x.md",
            tier=1,
            conditional_trigger=None,
            max_output_tokens=8192,
            full_mode_only=False,
            context_enrichment_eligible=True,
        )


def test_agentspec_rejects_invalid_tier() -> None:
    with pytest.raises(ValueError, match="tier"):
        AgentSpec(
            name="x",
            prompt_path="prompts/x.md",
            tier=3,
            conditional_trigger=None,
            max_output_tokens=8192,
            full_mode_only=False,
            context_enrichment_eligible=True,
        )


def test_agentspec_rejects_zero_tier() -> None:
    with pytest.raises(ValueError, match="tier"):
        AgentSpec(
            name="x",
            prompt_path="prompts/x.md",
            tier=0,
            conditional_trigger=None,
            max_output_tokens=8192,
            full_mode_only=False,
            context_enrichment_eligible=True,
        )


def test_agentspec_rejects_tokens_below_min() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        AgentSpec(
            name="x",
            prompt_path="prompts/x.md",
            tier=1,
            conditional_trigger=None,
            max_output_tokens=100,
            full_mode_only=False,
            context_enrichment_eligible=True,
        )


def test_agentspec_rejects_tokens_above_max() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        AgentSpec(
            name="x",
            prompt_path="prompts/x.md",
            tier=1,
            conditional_trigger=None,
            max_output_tokens=99999,
            full_mode_only=False,
            context_enrichment_eligible=True,
        )


# ---------------------------------------------------------------------------
# AGENTS list integrity
# ---------------------------------------------------------------------------

EXPECTED_AGENTS = {
    "pr-summarizer",
    "code-reviewer",
    "silent-failure-hunter",
    "architecture-reviewer",
    "security-reviewer",
    "blind-hunter",
    "edge-case-hunter",
    "adversarial-general",
}


def test_agents_contains_all_expected() -> None:
    names = {a.name for a in AGENTS}
    assert names == EXPECTED_AGENTS


def test_agents_names_unique() -> None:
    names = [a.name for a in AGENTS]
    assert len(names) == len(set(names)), "Duplicate agent names in AGENTS list"


def test_agents_prompt_paths_exist() -> None:
    """All prompt_path values must resolve to real files under the repo root."""
    here = os.path.dirname(__file__)
    repo_root = here
    for _ in range(6):
        if os.path.isdir(os.path.join(repo_root, "prompts")):
            break
        repo_root = os.path.dirname(repo_root)
    else:
        pytest.fail("Could not locate repo root containing 'prompts/' directory")

    for spec in AGENTS:
        full = os.path.join(repo_root, spec.prompt_path)
        assert os.path.isfile(full), f"Prompt file missing: {spec.prompt_path} (resolved: {full})"


def test_blind_hunter_not_context_enrichment_eligible() -> None:
    spec = get_agent("blind-hunter")
    assert spec.context_enrichment_eligible is False


def test_tier1_agents_not_full_mode_only() -> None:
    for spec in AGENTS:
        if spec.tier == 1:
            assert not spec.full_mode_only, (
                f"Tier 1 agent '{spec.name}' should not be full_mode_only"
            )


def test_tier2_agents_are_full_mode_only() -> None:
    for spec in AGENTS:
        if spec.tier == 2:
            assert spec.full_mode_only, (
                f"Tier 2 agent '{spec.name}' should be full_mode_only"
            )


def test_pr_summarizer_is_tier1() -> None:
    assert get_agent("pr-summarizer").tier == 1


def test_adversarial_general_no_conditional_trigger() -> None:
    spec = get_agent("adversarial-general")
    assert spec.conditional_trigger is None


def test_silent_failure_hunter_has_trigger() -> None:
    spec = get_agent("silent-failure-hunter")
    assert spec.conditional_trigger == "has_error_patterns"


# ---------------------------------------------------------------------------
# get_agent helper
# ---------------------------------------------------------------------------

def test_get_agent_returns_correct_spec() -> None:
    spec = get_agent("code-reviewer")
    assert spec.name == "code-reviewer"
    assert spec.tier == 1
    assert spec.full_mode_only is False


def test_get_agent_raises_on_unknown() -> None:
    with pytest.raises(KeyError):
        get_agent("nonexistent-agent")
