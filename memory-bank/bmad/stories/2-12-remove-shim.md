---
title: "E2.S12 — Remove compute handoff shim"
epic: 2
story: 12
status: review
github_issue: 227
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S12 — Remove compute handoff shim

## Summary

E1.S10 introduced a JSON tempfile handoff between the Python compute engine and the bash posting
scripts so Epic 1 could ship without Epic 2's posting layer. Now that S9–S11 implement the
posting layer in Python, the shim is redundant. This story wires the Python compute output
directly into the Python `VcsProvider` and removes the shim + the bash-side consumer code paths
that read the JSON tempfile.

## PRD Reference

2.FR-11.

## Acceptance Criteria

- [x] AC1: `ai_pr_review/cli.py` grows a unified `review` subcommand (or extends the existing one)
  that: compute → dispatch agents → select `VcsProvider` by env → post. No JSON tempfile handoff.
- [x] AC2: The bash-side code that invokes the Python compute engine + reads the JSON tempfile is
  deleted (or gated behind `AI_PR_REVIEW_ENGINE=bash` for the legacy path only).
- [x] AC3: `AI_PR_REVIEW_ENGINE=python` (set by default in Epic 4) runs the full Python pipeline,
  never writing the handoff tempfile.
- [x] AC4: `AI_PR_REVIEW_ENGINE=bash` continues to run the legacy bash pipeline unchanged — zero
  behavior change for soak users until Epic 4 flips the default.
- [x] AC5: Golden parity harness remains green on both engine values.
- [x] AC6: `mypy --strict` and `ruff check` clean.
- [x] AC7: Fixture count unchanged (S13 expands fixtures, this story does not).

## Tasks/Subtasks

- [x] T1: Identify the shim's surface area (git grep `handoff`, `COMPUTE_JSON`, `ai_pr_review` in
  bash scripts) and catalog every read site.
- [x] T2: Wire `ai_pr_review/cli.py` to call the provider factory directly.
- [x] T3: Delete the Python-writes-JSON → bash-reads-JSON round trip from the bash `review.sh` path
  when engine=python; keep the bash→bash path intact when engine=bash.
- [x] T4: Update `action.yml` if the engine switch semantics change (should not).
- [x] T5: Unit test for the direct call path; regression test for the legacy bash path.
- [x] T6: Golden parity harness run; no diffs above tolerance.
- [x] T7: Doc update in `docs/ARCHITECTURE.md` noting the shim removal.

## Dev Notes

### Why gate, not delete?

Epic 4 flips the default to Python and then Epic 5 deletes all bash. S12 does the structural
removal of the *handoff*, not of bash itself — so we keep the bash branch reachable via the env
var for soak testing.

## File List

- `ai_pr_review/cli.py` (modified)
- `review.sh` (modified — remove engine=python handoff branch)
- `docs/ARCHITECTURE.md` (modified)
- `memory-bank/bmad/stories/2-12-remove-shim.md` (this file)

## Dev Agent Record

### Implementation Plan

1. Provider factory in `ai_pr_review/vcs/__init__.py` — selects by `VCS_PROVIDER`,
   builds config + client from per-provider envs.
2. `ai_pr_review/orchestrate.py` — `run_review()` glues compute → dispatch
   (run_tier from S3) → extract findings → merge + suppress → outcome → post.
   Honors AC5 ordering: summary → findings → stale, with stale only running
   on full success.
3. `ai_pr_review/cli.py` grows a `review` subcommand that builds the provider,
   runs compute, evaluates conditional gates (S4), filters AGENTS by
   review_mode + fired gates, binds the LLM call, and invokes `run_review()`.
4. `review.sh` `AI_PR_REVIEW_ENGINE=python` branch now invokes
   `python3 -m ai_pr_review review` and exits — no JSON tempfile handoff.

### Completion Notes

- `_AsFindingLike` adapter bridges Finding's Literal severity to the outcome
  classifier's Protocol (severity: str). Protocol attrs are invariant under
  mypy strict; this is the cleanest fix.
- pr-summarizer is excluded from the generic dispatch path — `run_tier` raises
  for it deliberately. Summarizer integration is a follow-up; for now
  `summary_text` is empty when entering the orchestrator.
- The bash engine path is **unchanged** — 644 bats tests still pass. Epic 4
  flips the default; Epic 5 deletes the bash branch.
- Field-soak validation against the 3 live test repos is queued as a
  separate task (post-S13) per the agreement to land the structural change
  first and validate live afterwards.

### Test results

- `pytest tests/python/` — **664 passed** (6 new orchestrator tests + 12 new
  factory tests; 174 vcs total)
- `bats tests/*.bats` — **644 passed** (bash side unaffected)
- `mypy --strict ai_pr_review/` — no issues (47 source files)
- `ruff check ai_pr_review/ tests/python/` — all checks passed
- `shellcheck review.sh` — clean

## Change Log

- 2026-05-13: Created E2.S12 story — Remove compute handoff shim.
- 2026-05-13: Implementation complete. Provider factory + orchestrator +
  `review` subcommand + `review.sh` rewiring. 6 orchestrator tests + 12
  factory tests; 664 pytest + 644 bats green. mypy strict + ruff + shellcheck
  clean. Status → review. Live E2E validation queued post-S13.
