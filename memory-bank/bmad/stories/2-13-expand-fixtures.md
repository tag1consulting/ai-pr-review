---
title: "E2.S13 — Expand fixtures (failed-agent, competing-bot, incremental)"
epic: 2
story: 13
status: blocked-by-s9-s10-s11
github_issue: 228
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S13 — Expand fixtures (failed-agent, competing-bot, incremental)

## Summary

Record three new golden-harness fixture scenarios using the Epic 0 record mode. These scenarios
exercise the correctness properties that S9–S11 establish: agent failure tolerance, non-ownership
respect (another bot's comments), and per-agent watermark advance across incremental PR pushes.

## PRD Reference

2.AC-3, 2.AC-4.

## Acceptance Criteria

- [ ] AC1: **Failed-agent fixture** — a PR where one agent raises (e.g., LLM 429 after retries),
  the dispatch layer records a `FailedAgent`, other agents complete, the summary comment shows a
  "failed: blind-hunter" note, and posting still succeeds.
- [ ] AC2: **Competing-bot fixture** — a PR with pre-existing inline review comments from a
  different bot (e.g., dependabot, renovate). Marker-gated cleanup MUST leave those comments
  untouched across all three providers (GitHub, GitLab, Bitbucket tapes).
- [ ] AC3: **Incremental fixture** — two successive pushes to the same PR. First push posts N
  findings; second push only re-runs agents whose watermark is behind the new head SHA; summary
  comment's `sha=` marker advances to the new head.
- [ ] AC4: Each fixture lives under `tests/golden/fixtures/<scenario>/` following the Epic 0
  schema (tapes/, diff, config, expected.json).
- [ ] AC5: `tests/golden/diff_harness.py` runs green for all three new fixtures.
- [ ] AC6: Tolerances documented in `tests/golden/tolerances.md` if any minor drift (timestamps,
  jitter) is expected.
- [ ] AC7: CI parity workflow from Epic 0 picks up the new fixtures automatically.

## Tasks/Subtasks

- [ ] T1: Use `AI_PR_REVIEW_RECORD_DIR` + a real throwaway PR (this repo or ai-pr-review-test) to
  capture the failed-agent scenario. Trigger failure by temporarily setting a malformed provider
  API key for one agent.
- [ ] T2: Use the GitLab test repo to record the competing-bot scenario (pre-seed discussions from
  a second account, run review, verify non-ownership).
- [ ] T3: Record the incremental scenario by pushing twice to a live PR and capturing both runs as
  a single fixture with two "steps" subdirectories.
- [ ] T4: Scrub PII/tokens from recorded tapes (the `_redact_secrets` helper should already do this
  — verify).
- [ ] T5: Add expected-output snapshots + run `diff_harness.py` until green.
- [ ] T6: Document the three fixtures in `tests/golden/README.md`.

## Dev Notes

### Provider coverage

Each scenario ideally has tapes for all three providers. Start with GitHub (primary), add GitLab
where the scenario is naturally achievable there, Bitbucket last — Bitbucket's API coverage is
narrowest so some scenarios (incremental) may collapse to a single comment edit rather than a
richer flow.

### Why real tapes vs. synthetic?

Synthetic payloads would miss subtleties in real responses (pagination tokens, trailing whitespace,
null vs missing fields) — exactly the kind of drift the golden harness is meant to catch.

## File List

- `tests/golden/fixtures/failed-agent/` (new)
- `tests/golden/fixtures/competing-bot/` (new)
- `tests/golden/fixtures/incremental-update/` (new)
- `tests/golden/tolerances.md` (modified if needed)
- `tests/golden/README.md` (modified)
- `memory-bank/bmad/stories/2-13-expand-fixtures.md` (this file)

## Change Log

- 2026-05-13: Created E2.S13 story — Expand fixtures.
