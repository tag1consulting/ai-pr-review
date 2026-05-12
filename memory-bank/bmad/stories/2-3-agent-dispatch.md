---
title: "E2.S3 — Agent dispatch and parallelism"
epic: 2
story: 3
status: ready-for-review
github_issue: 218
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S3 — Agent dispatch and parallelism

## Summary

Implement `ai_pr_review/agents/dispatch.py` — the async fan-out layer that runs Tier 1
and Tier 2 agents via `asyncio.gather` with bounded concurrency. Ports `call_agent`,
`call_agent_bg`, `wait_tier_pids`, `collect_parallel_results`, `cache_priming_effective`,
and `effective_prompt` from `lib/agents.sh`. Failed agents are tracked in a typed
`FailedAgent` dataclass and returned alongside successes.

## PRD Reference

2.FR-3

## Acceptance Criteria

- [ ] AC1: `run_tier(agents, ...) -> tuple[list[AgentResult], list[FailedAgent]]` runs agents concurrently via `asyncio.gather` with `asyncio.Semaphore` (Tier 1: 3; Tier 2: 5).
- [ ] AC2: `AgentResult` carries `name`, `output` (str), `token_log` (TokenUsage), `truncated` (bool).
- [ ] AC3: `FailedAgent` carries `name`, `reason` (str), `exit_code` (int), `elapsed_ms` (int).
- [ ] AC4: Failed agents (non-zero exit from LLM client) are caught, recorded in `FailedAgent`, and do NOT raise — dispatch continues with remaining agents.
- [ ] AC5: `effective_prompt(agent_name, base_prompt_path, script_dir) -> Path` composes `_knowledge-cutoff.md` + `_trailer-findings.md` onto finding-producing agents; optionally appends `suggestion-addendum.md` when `AI_ENABLE_SUGGESTIONS != false`. pr-summarizer passes through unchanged.
- [ ] AC6: `cache_priming_effective() -> bool` returns `True` only when `AI_CACHE_PRIMING=true` AND provider is `anthropic` or `bedrock-proxy` AND `LLM_PROMPT_CACHING != false`.
- [ ] AC7: `mypy --strict` and `ruff check` clean on all new code.
- [ ] AC8: Unit tests cover: happy-path tier run, partial failure (one agent fails, others succeed), `effective_prompt` composition for each agent class, `cache_priming_effective` for all env-var combinations.

## Tasks/Subtasks

- [x] T1: Define `AgentResult` and `FailedAgent` dataclasses in `ai_pr_review/agents/dispatch.py`
  - [x] T1.1: `AgentResult(name: str, output: str, token_log: TokenUsage | None, truncated: bool)`
  - [x] T1.2: `FailedAgent(name: str, reason: str, exit_code: int, elapsed_ms: int)`
  - [x] T1.3: `TokenUsage(input: int, output: int, cache_creation: int, cache_read: int, model: str)` dataclass (or reuse from llm/ if already defined)
- [x] T2: Implement `run_tier(agents, llm_client, context, semaphore_size) -> tuple[list[AgentResult], list[FailedAgent]]`
  - [x] T2.1: Accept `agents: list[AgentSpec]`, a callable LLM invoker, a `DispatchContext` (diff, mode, script_dir, etc.), and `semaphore_size: int`
  - [x] T2.2: Build coroutines that call `effective_prompt()` then invoke the LLM client
  - [x] T2.3: Wrap each coroutine in try/except; on failure record `FailedAgent`, continue
  - [x] T2.4: Use `anyio.create_task_group()` gated by `anyio.CapacityLimiter` (works with both asyncio and trio backends)
  - [x] T2.5: Return `(successes, failures)` tuple
- [x] T3: Implement `effective_prompt(agent_name, base_prompt_path, script_dir, enable_suggestions) -> Path`
  - [x] T3.1: Port the bash sets: `agents_with_findings_trailer`, `agents_with_suggestion_addendum`
  - [x] T3.2: Concatenate base + `_knowledge-cutoff.md` + `_trailer-findings.md` into a tempfile for finding-producing agents
  - [x] T3.3: Optionally append `suggestion-addendum.md` when `enable_suggestions=True` and agent is in the suggestion set
  - [x] T3.4: pr-summarizer passes through unchanged (return base_prompt_path directly)
  - [x] T3.5: Missing trailer files: fall back to base prompt with a `WARNING` printed to stderr (match bash behavior)
- [x] T4: Implement `cache_priming_effective(provider, cache_priming_env, prompt_caching_env) -> bool`
  - [x] T4.1: Return `False` if `cache_priming_env` is not truthy
  - [x] T4.2: Return `False` if provider is not `anthropic` or `bedrock-proxy`
  - [x] T4.3: Return `False` if `prompt_caching_env` is explicitly `false`
  - [x] T4.4: Return `True` otherwise
- [x] T5: Write tests in `tests/python/agents/test_dispatch.py`
  - [x] T5.1: Test `run_tier` happy path — all agents succeed, results returned
  - [x] T5.2: Test `run_tier` partial failure — one agent raises, others succeed; `FailedAgent` recorded
  - [x] T5.3: Test `run_tier` semaphore — verify concurrency limit is respected (mock timing or mock semaphore)
  - [x] T5.4: Test `effective_prompt` for finding-producing agent (gets trailers)
  - [x] T5.5: Test `effective_prompt` for pr-summarizer (passes through unchanged)
  - [x] T5.6: Test `effective_prompt` with missing trailer (falls back to base, emits WARNING)
  - [x] T5.7: Test `effective_prompt` with `enable_suggestions=True` for suggestion-eligible agent
  - [x] T5.8: Test `effective_prompt` with `enable_suggestions=False` — no suggestion addendum
  - [x] T5.9: Test `cache_priming_effective` for all truthy/falsy combinations (provider, AI_CACHE_PRIMING, LLM_PROMPT_CACHING)
- [x] T6: Run full test suite + mypy + ruff; confirm clean

## Dev Notes

### Bash Reference: lib/agents.sh

The complete bash implementation lives in `lib/agents.sh` (365 lines). Key functions:

| Bash function | Python equivalent |
|---|---|
| `call_agent()` | `_run_single_agent()` coroutine (internal) |
| `call_agent_bg()` | same coroutine — `asyncio.gather` handles background |
| `wait_tier_pids()` | `await asyncio.gather(...)` |
| `collect_parallel_results()` | results returned from `run_tier()` directly |
| `cache_priming_effective()` | `cache_priming_effective()` function |
| `effective_prompt()` | `effective_prompt()` function |

### Prompt composition rules (from bash)

**Agents with findings trailer + knowledge-cutoff** (all 7 finding-producing agents):
`code-reviewer | silent-failure-hunter | security-reviewer | edge-case-hunter | blind-hunter | architecture-reviewer | adversarial-general`

**Agents with suggestion addendum** (when `AI_ENABLE_SUGGESTIONS=true`, default):
`code-reviewer | edge-case-hunter | security-reviewer | silent-failure-hunter | blind-hunter`

**pr-summarizer**: passes through unchanged — different output contract (no findings JSON).

Composition order: `base_prompt + _knowledge-cutoff.md + _trailer-findings.md + [suggestion-addendum.md]`

### DispatchContext

The function signature needs access to: `script_dir` (for resolving prompt paths), `mode` (quick/full), `diff_file` (path), plus env vars (`AI_ENABLE_SUGGESTIONS`, `AI_CACHE_PRIMING`, `AI_PROVIDER`, `LLM_PROMPT_CACHING`). Use a simple dataclass:

```python
@dataclass
class DispatchContext:
    script_dir: Path
    mode: str          # "quick" | "full"
    diff_path: Path
    provider: str
    enable_suggestions: bool = True
    cache_priming_env: str = "false"
    prompt_caching_env: str = "auto"
```

### LLM client interface

`run_tier` receives a callable conforming to the `LLMClient` interface from E2.S1 (`ai_pr_review/llm/`). For unit tests, use a mock/async coroutine that returns a canned `LLMResponse`.

### Token parsing

Bash parses `TOKENS: input=N output=N cache_creation=N cache_read=N model=...` from stderr. The Python LLM client (E2.S1) already writes these values to `LLMResponse.usage`. Map:
- `LLMResponse.usage.input_tokens` → `TokenUsage.input`
- `LLMResponse.usage.output_tokens` → `TokenUsage.output`
- `LLMResponse.usage.cache_creation_tokens` → `TokenUsage.cache_creation`
- `LLMResponse.usage.cache_read_tokens` → `TokenUsage.cache_read`

### Truncation handling

The bash `call_agent` touches `${output}.truncated` when the LLM client emits `TRUNCATED:true` on stderr. The Python `LLMResponse` should include a `truncated: bool` field (check E2.S1 implementation). If it does, propagate to `AgentResult.truncated`. If it doesn't, add the field.

### Concurrency limits

| Tier | Semaphore size | Source |
|---|---|---|
| 1 | 3 | `architecture-ai-pr-review.md` § Agent Dispatch & Concurrency |
| 2 | 5 | same |

These match current bash behavior (bash uses background PIDs but the concurrency is effectively 3 and 5 by design).

### Previous learnings from E2.S1 and E2.S2

- Use `from __future__ import annotations` for cleaner type hints
- Frozen dataclasses for immutable value objects; `__post_init__` for validation
- Keep `__post_init__` raising `ValueError` (not pydantic) — stdlib-only
- `asyncio` patterns: always use `asyncio.gather` with `return_exceptions=False` and wrap individual coroutines in try/except rather than using `return_exceptions=True` (cleaner failure isolation)
- Tests: use `pytest-anyio` or `asyncio.run()` for async test functions — check existing test setup in `pyproject.toml`

### E2.S1 LLMResponse structure (verify before implementing)

Check `ai_pr_review/llm/base.py` for the exact field names on `LLMResponse.usage`. The story above uses assumed names — verify against actual implementation.

## Dev Agent Record

### Implementation Plan

_To be filled during implementation_

### Debug Log

_Empty_

### Completion Notes

_Empty_

## File List

- `ai_pr_review/agents/dispatch.py` (new)
- `tests/python/agents/test_dispatch.py` (new)
- `memory-bank/bmad/stories/2-3-agent-dispatch.md` (new)

## Change Log

- 2026-05-12: Created E2.S3 story — Agent dispatch and parallelism.
