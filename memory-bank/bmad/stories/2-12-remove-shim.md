---
title: "E2.S12 — Remove compute handoff shim"
epic: 2
story: 12
status: blocked-by-s9-s10-s11
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

- [ ] AC1: `ai_pr_review/cli.py` grows a unified `review` subcommand (or extends the existing one)
  that: compute → dispatch agents → select `VcsProvider` by env → post. No JSON tempfile handoff.
- [ ] AC2: The bash-side code that invokes the Python compute engine + reads the JSON tempfile is
  deleted (or gated behind `AI_PR_REVIEW_ENGINE=bash` for the legacy path only).
- [ ] AC3: `AI_PR_REVIEW_ENGINE=python` (set by default in Epic 4) runs the full Python pipeline,
  never writing the handoff tempfile.
- [ ] AC4: `AI_PR_REVIEW_ENGINE=bash` continues to run the legacy bash pipeline unchanged — zero
  behavior change for soak users until Epic 4 flips the default.
- [ ] AC5: Golden parity harness remains green on both engine values.
- [ ] AC6: `mypy --strict` and `ruff check` clean.
- [ ] AC7: Fixture count unchanged (S13 expands fixtures, this story does not).

## Tasks/Subtasks

- [ ] T1: Identify the shim's surface area (git grep `handoff`, `COMPUTE_JSON`, `ai_pr_review` in
  bash scripts) and catalog every read site.
- [ ] T2: Wire `ai_pr_review/cli.py` to call the provider factory directly.
- [ ] T3: Delete the Python-writes-JSON → bash-reads-JSON round trip from the bash `review.sh` path
  when engine=python; keep the bash→bash path intact when engine=bash.
- [ ] T4: Update `action.yml` if the engine switch semantics change (should not).
- [ ] T5: Unit test for the direct call path; regression test for the legacy bash path.
- [ ] T6: Golden parity harness run; no diffs above tolerance.
- [ ] T7: Doc update in `docs/ARCHITECTURE.md` noting the shim removal.

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

## Change Log

- 2026-05-13: Created E2.S12 story — Remove compute handoff shim.
