---
title: "E2.S7 — Watermark policy (per-agent)"
epic: 2
story: 7
status: ready-for-review
github_issue: 222
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S7 — Watermark policy (per-agent)

## Summary

Implement `ai_pr_review/review/watermark.py` — a pure policy helper that decides **whether** and
**how** to advance the SHA watermark embedded in the summary comment marker, given the set of
agents that ran, the set that failed, and the current HEAD SHA. Fixes #182: today the bash marker
advances even when an agent failed, creating a permanent coverage gap for transient failures (a
security-reviewer crash would make the *next* incremental review skip the code it missed).

This story is **policy only** — no network I/O. S9 (GitHub provider) consumes the policy and
performs the actual `PATCH /issues/comments/:id`.

## PRD Reference

2.FR-7 — resolves #182

## Acceptance Criteria

- [x] AC1: `WatermarkPolicy` frozen dataclass with:
  - `advance_global: bool` — whether the global SHA marker may be updated
  - `new_global_sha: str | None` — the SHA to write (None when advance_global=False)
  - `per_agent: Mapping[str, str]` — map of agent-name → SHA for agents that succeeded
  - `body_explanation: str` — human-readable explanation naming the failed agents (empty string
    when all succeeded)
- [x] AC2: `decide_watermark_advance(head_sha: str, succeeded_agents: Sequence[str], failed_agents:
  Sequence[str], required_for_global: Sequence[str] | None = None) -> WatermarkPolicy`.
- [x] AC3: All agents succeeded → `advance_global=True`, `new_global_sha=head_sha`, `per_agent`
  maps every succeeded agent to head_sha, `body_explanation=""`.
- [x] AC4: Any agent in `required_for_global` failed → `advance_global=False`,
  `new_global_sha=None`, `per_agent` still maps every succeeded agent to head_sha.
  `body_explanation` names the failed required agents.
- [x] AC5: `required_for_global=None` defaults to "all agents that ran are required": any failure
  blocks global advance.
- [x] AC6: `required_for_global=[]` (explicit empty list) means "no agent is required for global
  advance": global always advances as long as there's a valid head_sha — per_agent still omits
  failures.
- [x] AC7: head_sha must be non-empty and match `^[0-9a-f]{7,40}$`. Invalid head_sha →
  `advance_global=False`, `new_global_sha=None`, explanation says why.
- [x] AC8: Agent names appearing in both `succeeded_agents` and `failed_agents` are treated as
  failed (conservative — a crash after partial success is still a failure).
- [x] AC9: `body_explanation` format when failures exist:
  `"Watermark held at previous SHA — the following agents did not complete: <comma-separated>.
  Re-run or next push will re-review from the previous watermark."`
- [x] AC10: `mypy --strict` and `ruff check` clean.
- [x] AC11: Unit tests cover: all success, one required failure, one non-required failure, empty
  agent lists, invalid SHA, ambiguous agent (succeeded + failed), and default-vs-empty `required`.

## Tasks/Subtasks

- [x] T1: Create `ai_pr_review/review/watermark.py`
  - [x] T1.1: Define `WatermarkPolicy` frozen dataclass
  - [x] T1.2: Implement `decide_watermark_advance()`
  - [x] T1.3: Implement `_is_valid_sha()` (regex at module level)
- [x] T2: Write tests in `tests/python/review/test_watermark.py`
  - [x] T2.1: All succeed → advance with head_sha
  - [x] T2.2: One required failure blocks global advance; explanation names it
  - [x] T2.3: Non-required failure (succeeded_agents ⊃ required set) → global advances
  - [x] T2.4: Default `required=None` means "all that ran are required"
  - [x] T2.5: Explicit `required=[]` means "none required" — global advances despite failures
  - [x] T2.6: Ambiguous agent (in both lists) → treated as failed
  - [x] T2.7: Invalid head_sha → no advance
  - [x] T2.8: Empty head_sha → no advance
  - [x] T2.9: `per_agent` map only contains succeeded agents
- [x] T3: Run full test suite + mypy + ruff; confirm clean

## Dev Notes

### Bash reference

From `post-review.sh::update_sha_marker` (lines 1148–1189): the bash version unconditionally
PATCHes the marker whenever `summary_ok=true`. The Python port splits the concern: `watermark.py`
decides the policy, and the GitHub provider (S9) performs the PATCH only when the policy
allows it.

### Per-agent watermark storage

For this story we return the per-agent map in the policy object but do NOT persist it to the PR
comment (the bash marker only stores one SHA). The per-agent map is the shape the VCS layer
consumes in S9 — how it's serialized into the comment marker is a later decision (could be a
JSON-encoded data attribute, a separate hidden comment, or #182's "more precise" suggestion).
For now: emit it as a structured return value; S9 may choose to log it and update only the
global marker.

### Required-agents default

The default policy (`required_for_global=None`) is the **conservative** option from #182's
suggested fix: "if any finding agent fails, do not update the global watermark." That's the
minimum correctness fix. Downstream callers (review.sh replacement) can override with a
narrower `required_for_global` list when they want to accept e.g. comment-analyzer failures.

### Previous learnings from E2.S1–S6

- `from __future__ import annotations`
- Frozen dataclasses; stdlib only
- Tests: `pytest`
- `mypy --strict` with `from collections.abc import Sequence, Mapping`

## File List

- `ai_pr_review/review/watermark.py` (new)
- `tests/python/review/test_watermark.py` (new)
- `memory-bank/bmad/stories/2-7-watermark-policy.md` (this file)

## Change Log

- 2026-05-13: Created E2.S7 story — Watermark policy (per-agent).
