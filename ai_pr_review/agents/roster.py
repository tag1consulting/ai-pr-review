"""Declarative agent roster — single source of truth for all LLM review agents.

Each AgentSpec encodes the properties previously scattered across review.sh,
lib/agents.sh, and lib/diff.sh. Adding a new agent is one list entry here.

conditional_trigger values (consumed by dispatch/gates layers):
  None                  — always run (within tier/mode constraints)
  "has_error_patterns"  — grep for catch/try/rescue/except/.catch() in diff
  "has_code_or_infra"   — skip when all changes are docs/meta only
  "has_security_patterns" — sec keywords or sec file paths in diff
  "has_control_flow"    — added lines contain control-flow keywords
  "no_prior_summary"    — skip on incremental runs (LAST_REVIEWED_SHA set)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

ConditionalTrigger = Literal[
    "has_error_patterns",
    "has_code_or_infra",
    "has_security_patterns",
    "has_control_flow",
    "no_prior_summary",
]

_VALID_TRIGGERS: frozenset[str] = frozenset(get_args(ConditionalTrigger))


@dataclass(frozen=True)
class AgentSpec:
    """Immutable specification for a single LLM review agent."""

    name: str
    prompt_path: str
    tier: int
    conditional_trigger: ConditionalTrigger | None
    max_output_tokens: int
    full_mode_only: bool
    context_enrichment_eligible: bool
    separately_dispatched: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("AgentSpec.name must be non-empty")
        if self.tier not in (1, 2):
            raise ValueError(f"AgentSpec.tier must be 1 or 2, got {self.tier!r}")
        if not (256 <= self.max_output_tokens <= 65536):
            raise ValueError(
                f"AgentSpec.max_output_tokens must be in [256, 65536], "
                f"got {self.max_output_tokens}"
            )
        if self.conditional_trigger is not None and self.conditional_trigger not in _VALID_TRIGGERS:
            raise ValueError(
                f"AgentSpec.conditional_trigger {self.conditional_trigger!r} is not a known gate key. "
                f"Valid values: {sorted(_VALID_TRIGGERS)}"
            )


# ---------------------------------------------------------------------------
# Agent roster — extracted from review.sh + lib/diff.sh
# ---------------------------------------------------------------------------
# max_output_tokens uses the per-agent default (32768 for prose agents, 4096 for
# issue-linker). The dispatch layer may apply a global override via
# AI_MAX_TOKENS_PER_AGENT; this field provides the per-agent budget for cost-table
# rendering (2.NFR-4).

AGENTS: list[AgentSpec] = [
    # --- Tier 1: run in both quick and full mode ---
    AgentSpec(
        name="pr-summarizer",
        prompt_path="prompts/pr-summarizer.md",
        tier=1,
        conditional_trigger="no_prior_summary",
        max_output_tokens=16384,
        full_mode_only=False,
        context_enrichment_eligible=True,
        separately_dispatched=True,
    ),
    AgentSpec(
        name="code-reviewer",
        prompt_path="prompts/code-reviewer.md",
        tier=1,
        conditional_trigger=None,
        max_output_tokens=32768,
        full_mode_only=False,
        context_enrichment_eligible=True,
    ),
    AgentSpec(
        name="silent-failure-hunter",
        prompt_path="prompts/silent-failure-hunter.md",
        tier=1,
        conditional_trigger="has_error_patterns",
        max_output_tokens=32768,
        full_mode_only=False,
        context_enrichment_eligible=True,
    ),
    # --- Tier 2: full mode only ---
    AgentSpec(
        name="architecture-reviewer",
        prompt_path="prompts/architecture-reviewer.md",
        tier=2,
        conditional_trigger="has_code_or_infra",
        max_output_tokens=32768,
        full_mode_only=True,
        context_enrichment_eligible=True,
    ),
    AgentSpec(
        name="security-reviewer",
        prompt_path="prompts/security-reviewer.md",
        tier=2,
        conditional_trigger="has_security_patterns",
        max_output_tokens=32768,
        full_mode_only=True,
        context_enrichment_eligible=True,
    ),
    AgentSpec(
        # Diff-only by design (#189): no symbol context injected.
        name="blind-hunter",
        prompt_path="prompts/blind-hunter.md",
        tier=2,
        conditional_trigger=None,
        max_output_tokens=32768,
        full_mode_only=True,
        context_enrichment_eligible=False,
    ),
    AgentSpec(
        name="edge-case-hunter",
        prompt_path="prompts/edge-case-hunter.md",
        tier=2,
        conditional_trigger="has_control_flow",
        max_output_tokens=32768,
        full_mode_only=True,
        context_enrichment_eligible=True,
    ),
    AgentSpec(
        name="adversarial-general",
        prompt_path="prompts/adversarial-general.md",
        tier=2,
        conditional_trigger=None,
        max_output_tokens=32768,
        full_mode_only=True,
        context_enrichment_eligible=True,
    ),
    AgentSpec(
        # GitHub-only: discovers related issues/PRs and assesses resolution.
        # Pre-fetches the open-issue list via ``gh issue list`` in Python before the
        # LLM call, then injects it as plain text so the model can cite real titles
        # without any tool-calling loop.  Dispatched separately via
        # _run_issue_linker() in cli.py; excluded from generic run_tier dispatch via
        # separately_dispatched=True.
        name="issue-linker",
        prompt_path="prompts/issue-linker.md",
        tier=2,
        conditional_trigger=None,
        max_output_tokens=4096,
        full_mode_only=True,
        context_enrichment_eligible=False,
        separately_dispatched=True,
    ),
]

_AGENTS_BY_NAME: dict[str, AgentSpec] = {a.name: a for a in AGENTS}

if len(_AGENTS_BY_NAME) != len(AGENTS):
    _seen: set[str] = set()
    _dups: list[str] = []
    for _a in AGENTS:
        if _a.name in _seen:
            _dups.append(_a.name)
        _seen.add(_a.name)
    raise ValueError(f"Duplicate agent names in AGENTS roster: {_dups}")


def get_agent(name: str) -> AgentSpec:
    """Return the AgentSpec for the given name, raising KeyError if not found."""
    return _AGENTS_BY_NAME[name]
