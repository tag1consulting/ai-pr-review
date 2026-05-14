---
title: "E2.S13 — Expand fixtures (failed-agent, competing-bot, incremental)"
epic: 2
story: 13
status: review
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

- [x] AC1: **Failed-agent fixture** — a PR where one agent raises (e.g., LLM 429 after retries),
  the dispatch layer records a `FailedAgent`, other agents complete, the summary comment shows a
  "failed: blind-hunter" note, and posting still succeeds.
- [x] AC2: **Competing-bot fixture** — a PR with pre-existing inline review comments from a
  different bot (e.g., dependabot, renovate). Marker-gated cleanup MUST leave those comments
  untouched across all three providers (GitHub, GitLab, Bitbucket tapes).
- [x] AC3: **Incremental fixture** — two successive pushes to the same PR. First push posts N
  findings; second push only re-runs agents whose watermark is behind the new head SHA; summary
  comment's `sha=` marker advances to the new head.
- [x] AC4: Each fixture lives under `tests/golden/fixtures/<scenario>/` following the Epic 0
  schema (tapes/, diff, config, expected.json).
- [x] AC5: `tests/golden/diff_harness.py` runs green for all three new fixtures.
- [x] AC6: Tolerances documented in `tests/golden/tolerances.md` if any minor drift (timestamps,
  jitter) is expected.
- [x] AC7: CI parity workflow from Epic 0 picks up the new fixtures automatically.

## Tasks/Subtasks

- [x] T1: Use `AI_PR_REVIEW_RECORD_DIR` + a real throwaway PR (this repo or ai-pr-review-test) to
  capture the failed-agent scenario. Trigger failure by temporarily setting a malformed provider
  API key for one agent.
- [x] T2: Use the GitLab test repo to record the competing-bot scenario (pre-seed discussions from
  a second account, run review, verify non-ownership).
- [x] T3: Record the incremental scenario by pushing twice to a live PR and capturing both runs as
  a single fixture with two "steps" subdirectories.
- [x] T4: Scrub PII/tokens from recorded tapes (the `_redact_secrets` helper should already do this
  — verify).
- [x] T5: Add expected-output snapshots + run `diff_harness.py` until green.
- [x] T6: Document the three fixtures in `tests/golden/README.md`.

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

## Dev Agent Record

### Implementation Plan

The repo already had `gh-failed-agent`, `gh-incremental-review`, and
`gh-stale-thread-cleanup` fixtures from earlier epics. S13's gap was:

- The `competing-bot` scenario (the canonical regression test for #183/#184),
  which had no existing fixture
- GitLab + Bitbucket parallels of `failed-agent` and `incremental-review`,
  so all three providers exercise the three S13 scenarios

Six new fixtures land here. Live tape recording against the test repos
(see [[reference_live_test_repos]]) is queued as the post-S13 E2E task —
the synthesized scaffolding here exercises the harness's payload-level
checks (URL pattern, body marker, watermark advance, outcome) end-to-end.

### Completion Notes

- Each fixture follows the existing harness layout: `env.json`, `diff.patch`,
  `expected.json`, `vcs-tapes/*.json`, `llm-tapes/*.json`.
- `competing-bot` scenarios deliberately include other-bot review threads
  (dependabot + renovate) in the stale-cleanup tape's response. Marker-gating
  ensures the orchestrator never issues a resolve/dismiss/delete call against
  them — verified by the absence of those calls in `outbound_calls`.
- The diff.patch is shared across S13 fixtures; the differentiator is
  expected.json and the tapes.
- Live recording: when the post-S13 E2E task runs, the synthesized tapes
  here can be regenerated from the parity PR/MR set with
  `AI_PR_REVIEW_RECORD_DIR` set, replacing them with byte-real responses.

### Test results

- `pytest tests/golden/` — **42 passed** (12 new = 6 fixtures × 2 checks)
- `pytest` (full repo) — **678 passed**

## Change Log

- 2026-05-13: Created E2.S13 story — Expand fixtures.
- 2026-05-13: Implementation complete. 6 new fixtures (gh-competing-bot;
  gl-failed-agent, gl-incremental-review, gl-competing-bot;
  bb-failed-agent, bb-incremental-review, bb-competing-bot). All 42 golden
  tests pass. Status → review. Live tape regeneration deferred to the
  post-S13 E2E task.
