"""Tests for ai_pr_review.agents.roster."""

from __future__ import annotations

import os

import pytest

from ai_pr_review.agents.roster import AGENT_NAMES, AGENTS, AgentSpec, agent_allowed, get_agent

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


def test_agentspec_rejects_unknown_conditional_trigger() -> None:
    with pytest.raises(ValueError, match="conditional_trigger"):
        AgentSpec(
            name="x",
            prompt_path="prompts/x.md",
            tier=1,
            conditional_trigger="typo_trigger",  # type: ignore[arg-type]
            max_output_tokens=8192,
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
    "issue-linker",
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


_PROSE_AGENTS = {
    "code-reviewer",
    "silent-failure-hunter",
    "architecture-reviewer",
    "security-reviewer",
    "blind-hunter",
    "edge-case-hunter",
    "adversarial-general",
}


def test_prose_agents_have_raised_token_budget() -> None:
    """Prose-heavy finding agents must have max_output_tokens=32768 (issue #430)."""
    for name in _PROSE_AGENTS:
        spec = get_agent(name)
        assert spec.max_output_tokens == 32768, (
            f"Expected {name} max_output_tokens=32768, got {spec.max_output_tokens}"
        )


def test_issue_linker_has_small_token_budget() -> None:
    """issue-linker is not prose-heavy; its budget should remain 4096."""
    spec = get_agent("issue-linker")
    assert spec.max_output_tokens == 4096


def test_get_agent_raises_on_unknown() -> None:
    with pytest.raises(KeyError):
        get_agent("nonexistent-agent")


def test_separately_dispatched_agents() -> None:
    separate = {a.name for a in AGENTS if a.separately_dispatched}
    assert "pr-summarizer" in separate
    assert "issue-linker" in separate
    assert "code-reviewer" not in separate


def test_generic_dispatch_excludes_separately_dispatched() -> None:
    generic = [a for a in AGENTS if not a.separately_dispatched]
    names = {a.name for a in generic}
    assert "pr-summarizer" not in names
    assert "issue-linker" not in names
    assert "code-reviewer" in names


# ---------------------------------------------------------------------------
# AGENT_NAMES canonical set
# ---------------------------------------------------------------------------


def test_agent_names_matches_roster() -> None:
    """AGENT_NAMES must exactly mirror the roster — no extras, no gaps."""
    assert {a.name for a in AGENTS} == AGENT_NAMES


def test_agent_names_covers_separately_dispatched() -> None:
    """pr-summarizer and issue-linker must be in AGENT_NAMES for validation."""
    assert "pr-summarizer" in AGENT_NAMES
    assert "issue-linker" in AGENT_NAMES


# ---------------------------------------------------------------------------
# agent_allowed: allow/deny truth table
# ---------------------------------------------------------------------------


def test_agent_allowed_both_empty_permits_all() -> None:
    """Empty allowlist + empty denylist => every agent is permitted (no-op default)."""
    for name in AGENT_NAMES:
        assert agent_allowed(name, (), ()) is True


def test_agent_allowed_allowlist_restricts_to_listed() -> None:
    """Non-empty allowlist permits only names in the allowlist."""
    allow = ("code-reviewer", "edge-case-hunter")
    for name in AGENT_NAMES:
        expected = name in allow
        assert agent_allowed(name, allow, ()) is expected


def test_agent_allowed_denylist_blocks_listed() -> None:
    """Denylist blocks the named agents; all others are permitted."""
    deny = ("adversarial-general", "blind-hunter")
    for name in AGENT_NAMES:
        expected = name not in deny
        assert agent_allowed(name, (), deny) is expected


def test_agent_allowed_allowlist_takes_precedence_over_denylist() -> None:
    """When allowlist is set, denylist is ignored entirely."""
    allow = ("code-reviewer",)
    deny = ("code-reviewer",)  # deny would block it, but allowlist takes precedence
    # code-reviewer IS in the allowlist, so it should be permitted despite the deny
    assert agent_allowed("code-reviewer", allow, deny) is True
    # edge-case-hunter is NOT in the allowlist, so it must be blocked
    assert agent_allowed("edge-case-hunter", allow, deny) is False


def test_agent_allowed_frozenset_inputs() -> None:
    """agent_allowed accepts frozenset inputs as well as tuples."""
    allow: frozenset[str] = frozenset({"code-reviewer"})
    deny: frozenset[str] = frozenset()
    assert agent_allowed("code-reviewer", allow, deny) is True
    assert agent_allowed("blind-hunter", allow, deny) is False
