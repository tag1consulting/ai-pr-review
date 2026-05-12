---
title: "E2.S2 — Agent roster registry"
epic: 2
story: 2
status: review
github_issue: 217
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S2 — Agent roster registry

## Summary

Define `ai_pr_review/agents/roster.py` with a typed `AgentSpec` dataclass and a single `AGENTS` list that enumerates every LLM review agent. Replaces the dual-path dispatch bash pattern where agent properties are scattered across `review.sh`, `lib/agents.sh`, and `lib/diff.sh`.

## PRD Reference

2.FR-2

## Acceptance Criteria

- [ ] AC1: One `AGENTS` list enumerates every agent (pr-summarizer, code-reviewer, silent-failure-hunter, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general).
- [ ] AC2: Adding a new agent is one file edit (add one `AgentSpec` to `AGENTS`).
- [ ] AC3: Per-agent `max_output_tokens` field populated and accessible (enables cost-log rendering per 2.NFR-4).
- [ ] AC4: `blind-hunter` marked `context_enrichment_eligible=False` per #189 design.
- [ ] AC5: `mypy --strict` and `ruff check` clean on all new code.
- [ ] AC6: Unit tests for `AgentSpec` field validation and `AGENTS` list integrity.

## Tasks/Subtasks

- [x] T1: Create `ai_pr_review/agents/__init__.py` (empty package marker)
- [x] T2: Implement `AgentSpec` dataclass in `ai_pr_review/agents/roster.py`
  - [x] T2.1: Fields: `name: str`, `prompt_path: str`, `tier: Literal[1, 2]`, `conditional_trigger: str | None`, `max_output_tokens: int`, `full_mode_only: bool`, `context_enrichment_eligible: bool`
  - [x] T2.2: `__post_init__` validation: name non-empty, tier in {1,2}, max_output_tokens in [256, 65536]
- [x] T3: Populate `AGENTS: list[AgentSpec]` with all 8 agents, properties extracted from review.sh/lib/diff.sh
  - [x] T3.1: Tier 1 agents: pr-summarizer, code-reviewer, silent-failure-hunter
  - [x] T3.2: Tier 2 agents: architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general
- [x] T4: Add helper `get_agent(name: str) -> AgentSpec` that raises `KeyError` on unknown name
- [x] T5: Write tests in `tests/python/agents/test_roster.py`
  - [x] T5.1: Test `AgentSpec` validation rejects bad inputs
  - [x] T5.2: Test `AGENTS` list integrity (all names unique, all prompt paths exist, blind-hunter not context_enrichment_eligible)
  - [x] T5.3: Test `get_agent()` happy path and KeyError
- [x] T6: Run full test suite + mypy + ruff; confirm clean

## Dev Notes

### Agent Properties Extracted from review.sh + lib/diff.sh

| Agent | Tier | conditional_trigger | full_mode_only | context_enrichment_eligible | Notes |
|---|---|---|---|---|---|
| pr-summarizer | 1 | `"no_prior_summary"` | False | True | Skipped on incremental runs (LAST_REVIEWED_SHA set); handled by dispatch, not roster |
| code-reviewer | 1 | None | False | True | Always runs; cache-priming primer in parallel mode |
| silent-failure-hunter | 1 | `"has_error_patterns"` | False | True | Gate: grep for catch/if err/try/rescue/Result/unwrap/except/.catch() in diff |
| architecture-reviewer | 2 | `"has_code_or_infra"` | True | True | Gate: skip when all changes are docs/meta only |
| security-reviewer | 2 | `"has_security_patterns"` | True | True | Gate: sec keywords OR sec file paths in diff |
| blind-hunter | 2 | None | True | **False** | Diff-only by design (#189); uses BLIND_MSG not CODE_CONTEXT_MSG |
| edge-case-hunter | 2 | `"has_control_flow"` | True | True | Gate: added lines contain control-flow keywords |
| adversarial-general | 2 | None | True | True | Always runs in full mode |

### Default max_output_tokens

Bash uses a single `AI_MAX_TOKENS_PER_AGENT` env var (default 8192 quick, 16384 full). For the roster, encode the **full-mode default** (16384) as the per-agent budget since dispatch will be able to override it. This matches 2.NFR-4: "per-agent token budgets visible in cost table".

### prompt_path convention

Paths are **relative to the repo root**: `"prompts/<agent-name>.md"`. The dispatch layer resolves them against the package root at runtime. This matches the bash `${SCRIPT_DIR}/prompts/<agent-name>.md` pattern.

### AgentSpec is a dataclass (not pydantic)

Epic 1 used plain dataclasses for `LLMRequest`/`LLMResponse`. Keep the same pattern for consistency — no pydantic dependency in the agents package.

### Previous learnings from E2.S1

- Use `from __future__ import annotations` for cleaner type hints
- `Literal[1, 2]` requires `from typing import Literal` (or `from __future__ import annotations` + runtime check in `__post_init__`)
- Keep `__post_init__` validation raising `ValueError` (not pydantic `ValidationError`) so it stays stdlib-only

## Dev Agent Record

### Implementation Plan

Straightforward data-definition story. Steps:
1. Create `agents/` package
2. Implement `AgentSpec` with validation
3. Populate `AGENTS` list from extracted properties above
4. Add `get_agent()` helper
5. Write tests (failing first, then implement)
6. Run full suite + mypy + ruff

### Debug Log

_Empty_

### Completion Notes

Implemented `AgentSpec` frozen dataclass with `__post_init__` validation (name non-empty, tier in {1,2}, max_output_tokens in [256,65536]). Populated all 8 agents from properties extracted from review.sh and lib/diff.sh. `blind-hunter` explicitly marked `context_enrichment_eligible=False` per #189. `get_agent()` helper raises `KeyError` on unknown name. 17 tests across validation, list integrity, and helper — all green. 262/262 full suite passing. mypy --strict and ruff clean.

## File List

- `ai_pr_review/agents/__init__.py` (new)
- `ai_pr_review/agents/roster.py` (new)
- `tests/python/agents/__init__.py` (new)
- `tests/python/agents/test_roster.py` (new)
- `memory-bank/bmad/stories/2-2-agent-roster.md` (new)

## Change Log

- 2026-05-12: Implemented E2.S2 — Agent roster registry. Created `AgentSpec` dataclass and `AGENTS` list with all 8 agents, `get_agent()` helper, and 17-test suite. All ACs satisfied.
