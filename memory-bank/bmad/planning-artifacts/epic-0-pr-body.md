# Epic 0 PR body — Golden Parity Harness (Payload-Level)

> **Not a committed file.** Local draft under `memory-bank/bmad/` (gitignored). Copy into the PR body when ready, edit down to what's implemented at PR time.

---

## Summary

Epic 0 of the [Python rework initiative](https://github.com/tag1consulting/ai-pr-review/issues/194). Builds the payload-level regression oracle — no Python engine code, pure test infrastructure — so Epics 1–5 have a real regression gate from day one.

**Milestone**: [Epic 0: Golden Parity Harness](https://github.com/tag1consulting/ai-pr-review/milestone/1)

## Why this first

A naive findings-only parity check would let inline-comment regressions slip through — the bug class the rewrite is supposed to *fix*. Shipping a payload-level harness before any engine change gives the rewrite a real oracle.

## What this PR delivers

- [ ] `AI_PR_REVIEW_RECORD_DIR` env-gated recording of LLM + VCS API calls — #200
- [ ] 10+ fixture corpus spanning diversity checklist — #201
- [ ] `tests/golden/diff_harness.py` with payload-level assertions — #202
- [ ] `tests/golden/inline_eligibility.py` oracle — #203
- [ ] `tests/golden/config_matrix.md` — authoritative env-var enumeration — #204
- [ ] `.github/workflows/parity.yml` CI gate — #205

## Acceptance criteria (from Epic issue)

- Bash engine replays cleanly against all fixtures with zero payload diffs (self-consistency).
- Harness asserts on findings JSON, outbound POST bodies, watermark transitions, and thread-resolution calls.
- Inline-eligibility oracle green on every fixture where bash posts inline comments.
- Config parity matrix covers every env var from `docs/configuration.md`.
- Parity CI workflow runs on every PR and gates merges.
- **No user-visible behavior change on the bash engine.**

## How it was tested

- `pytest tests/golden/` green (bash-vs-bash self-consistency).
- `.github/workflows/parity.yml` runs on this PR and gates.
- Manual smoke: full review run with `AI_PR_REVIEW_RECORD_DIR=<tmp>` on a real PR produces fixture files that replay cleanly.

## Risk assessment

| Risk | Mitigation |
|---|---|
| Recording leaks secrets | CI lint on recorded fixtures scans for known secret patterns; fails build on detection. |
| Non-determinism in tapes (timestamps, request IDs) | Documented in `tests/golden/tolerances.md`. |
| Fixture drift as bash ships patches | Freeze non-critical bash changes during Epics 0–2; re-record fixtures on behavioral bash change. |

## Closes

No existing issues close with this PR. Sets up infrastructure for Epic 1+ to close the bulk of folded issues.

## Next epic

[Epic 1: Python Core (Compute Only)](https://github.com/tag1consulting/ai-pr-review/issues/195) — port config, diff, findings, analyzer bridge to Python. Blocked on this PR.

---

## `/comprehensive-review` gate

Per standing workspace policy, run `/comprehensive-review` before merge.

## Co-author note

Per standing workspace policy, this PR body contains no Claude/AI attribution.
