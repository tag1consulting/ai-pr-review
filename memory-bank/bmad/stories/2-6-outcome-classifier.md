---
title: "E2.S6 — Outcome classifier (single source)"
epic: 2
story: 6
status: ready-for-review
github_issue: 221
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S6 — Outcome classifier (single source)

## Summary

Implement `ai_pr_review/review/outcome.py` — a single `classify_review_outcome()` helper replacing
the three duplicated risk-classification code paths in `post-review.sh`, `post-review-gitlab.sh`,
`post-review-bitbucket.sh`, and `vcs/common.sh::classify_risk`. The helper returns a typed
`ReviewOutcome` consumed by all three VCS provider implementations (S9–S11). The critical policy
change from the bash version: **any failed finding-producing agent forces `may_approve=False` and
`incomplete=True`**, regardless of the severity of remaining findings. This closes the loophole where
a Low/Medium-only review could APPROVE even when a finding agent crashed.

## PRD Reference

2.FR-6 — resolves #181, #192

## Acceptance Criteria

- [x] AC1: `ReviewOutcome` is a frozen dataclass with fields:
  - `risk: Literal["None", "Low", "Medium", "High", "Critical", "Unknown"]`
  - `event: Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"]`
  - `may_approve: bool`
  - `incomplete: bool`
  - `finding_total: int`
- [x] AC2: `classify_review_outcome(findings: Sequence[Finding], failed_agents: Sequence[str], mode:
  str) -> ReviewOutcome` returns a single typed outcome.
- [x] AC3: Zero findings + no failed agents → `risk="None"`, `event="APPROVE"`, `may_approve=True`,
  `incomplete=False`.
- [x] AC4: Zero findings + any failed agent → `risk="Unknown"`, `event="COMMENT"`,
  `may_approve=False`, `incomplete=True`.
- [x] AC5: At least one Critical finding → `risk="Critical"`, `event="REQUEST_CHANGES"`,
  `may_approve=False`, `incomplete=<failed_agents nonempty>`.
- [x] AC6: At least one High finding (no Critical) → `risk="High"`, `event="REQUEST_CHANGES"`,
  `may_approve=False`.
- [x] AC7: Only Medium findings (no Critical/High) + no failed agents →
  `risk="Medium"`, `event="APPROVE"`, `may_approve=True`, `incomplete=False`.
- [x] AC8: Only Low findings + no failed agents → `risk="Low"`, `event="APPROVE"`, `may_approve=True`.
- [x] AC9: **Policy**: Any failed finding agent forces `may_approve=False` and `incomplete=True`.
  When this overrides an APPROVE-eligible severity (Medium/Low), the event downgrades to `"COMMENT"`.
  Critical/High remain `"REQUEST_CHANGES"` (they were never going to approve anyway).
- [x] AC10: Severity comparison is case-insensitive (`"critical"`, `"Critical"`, `"CRITICAL"` all
  match).
- [x] AC11: Unknown severity values (e.g. `"info"`, `"warning"`) are ignored for risk escalation —
  they don't count toward Critical/High gating but also don't downgrade. `finding_total` still
  includes them.
- [x] AC12: `mode` parameter is accepted but currently unused; reserved for future policies
  (e.g. quick mode allowing APPROVE at Medium). Passed through for logging only.
- [x] AC13: `mypy --strict` and `ruff check` clean.
- [x] AC14: Unit tests cover: empty, only-Low, only-Medium, High-present, Critical-present,
  failed-agent-only, failed-agent-with-Low, failed-agent-with-Critical, mixed-case severity,
  unknown severity, and the event-downgrade path (Medium + failure → COMMENT).

## Tasks/Subtasks

- [x] T1: Create `ai_pr_review/review/` package with `__init__.py` and `outcome.py`
  - [x] T1.1: Define `Finding` protocol/typed-dict (minimum: `severity: str`). May reuse any
    existing Finding type from the analyzers/findings package — prefer reuse over redefinition.
  - [x] T1.2: Define `ReviewOutcome` frozen dataclass
  - [x] T1.3: Implement `classify_review_outcome()`
- [x] T2: Write tests in `tests/python/review/test_outcome.py`
  - [x] T2.1: Empty findings + no failures → APPROVE/None
  - [x] T2.2: Empty findings + failures → COMMENT/Unknown, incomplete=True
  - [x] T2.3: Critical present → REQUEST_CHANGES/Critical
  - [x] T2.4: High only → REQUEST_CHANGES/High
  - [x] T2.5: Medium only → APPROVE/Medium
  - [x] T2.6: Low only → APPROVE/Low
  - [x] T2.7: Medium + failed agent → COMMENT/Medium, may_approve=False, incomplete=True
  - [x] T2.8: Critical + failed agent → REQUEST_CHANGES, incomplete=True
  - [x] T2.9: Mixed-case severity handled
  - [x] T2.10: Unknown severity doesn't trigger Critical/High gating
- [x] T3: Run full test suite + mypy + ruff; confirm clean

## Dev Notes

### Bash reference

From `vcs/common.sh::classify_risk` and `post-review.sh:890-920`:

```bash
# Bash had the bug: Medium/Low could APPROVE even with failed agents.
# Python implementation MUST NOT reproduce this — the failure always blocks approve.
if [[ "$finding_total" -eq 0 && -z "$failed_agents_env" ]]; then
  overall_risk="None"; review_event="APPROVE"
elif [[ "$finding_total" -eq 0 ]]; then
  overall_risk="Unknown"; review_event="COMMENT"
elif critical_present; then
  overall_risk="Critical"; review_event="REQUEST_CHANGES"
# ...
```

### Finding type

For now, use a minimal `Finding` protocol or an inline runtime check — we only need `severity`.
Look at `ai_pr_review/findings/` (if present) for an existing type before defining a new one.
If nothing exists yet, define a small `@runtime_checkable` Protocol in `outcome.py` and keep it
open-ended for S7/S9-S11 to extend.

### Previous learnings from E2.S1–S5

- `from __future__ import annotations`
- Frozen dataclasses; stdlib only
- Tests: `pytest`
- `mypy --strict` with `from collections.abc import Sequence` (not `typing.Sequence`)

## File List

- `ai_pr_review/review/__init__.py` (new)
- `ai_pr_review/review/outcome.py` (new)
- `tests/python/review/__init__.py` (new)
- `tests/python/review/test_outcome.py` (new)
- `memory-bank/bmad/stories/2-6-outcome-classifier.md` (this file)

## Change Log

- 2026-05-12: Created E2.S6 story — Outcome classifier (single source).
