---
title: "E2.S9 — VCS protocol + GitHub provider"
epic: 2
story: 9
status: ready-for-dev
github_issue: 224
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S9 — VCS protocol + GitHub provider

## Summary

Define the shared VCS provider protocol in `ai_pr_review/vcs/protocol.py` and implement the first
concrete provider, `ai_pr_review/vcs/github.py`, by porting `post-review.sh` (1,207 LoC) and the
relevant helpers from `vcs/common.sh`. The GitHub provider is the reference implementation — S10
(GitLab) and S11 (Bitbucket) must satisfy the same protocol.

All stale cleanup MUST be gated by the inline marker from E2.S8 (closes #183, #184). Cleanup runs
**after** a successful post, not before (2.FR-10).

## PRD Reference

2.FR-9 (VCS protocol + provider implementations), 2.FR-10 (marker-gated cleanup order) — closes #183, #184

## Acceptance Criteria

- [ ] AC1: `ai_pr_review/vcs/protocol.py` defines a `VcsProvider` `typing.Protocol` with:
  - `post_summary(summary_body: str, head_sha: str) -> SummaryResult`
  - `post_findings(findings: list[Finding], diff: DiffContext, head_sha: str) -> FindingsResult`
  - `resolve_stale(head_sha: str) -> StaleResult` (marker-gated)
  - `get_last_reviewed_sha(pr_number: int) -> str | None`
  - `post_skip_comment(reason: str) -> None`
  - Typed return dataclasses carry posted/skipped/error counts and optional error messages.
- [ ] AC2: `ai_pr_review/vcs/github.py` implements `VcsProvider` for GitHub REST + GraphQL:
  - Summary comment upsert (keyed by `SUMMARY_MARKER_PREFIX` — one per PR).
  - Findings posted as a PR review with inline comments, each containing `INLINE_MARKER`.
  - Stale thread resolution via GraphQL `resolveReviewThread`, filtered by `has_inline_marker(body)`
    AND author match (defense in depth).
  - Stale review dismissal for old reviews from this bot, again gated by marker.
  - SHA watermark advance via `replace_summary_sha` on the existing summary comment (2.FR-7 semantics
    already encoded in E2.S7 — this story *consumes* that policy).
- [ ] AC3: `gh_api_retry` equivalent: retry 502/503/429/ETIMEDOUT up to 3× with exponential backoff
  + jitter. Unit tests with stubbed transport.
- [ ] AC4: VCS tape recording honored when `AI_PR_REVIEW_RECORD_DIR` is set (compatible with the
  Epic 0 golden harness tape format — method, URL, request body, response body, sequence number).
- [ ] AC5: Cleanup order invariant — `post_summary` and `post_findings` MUST succeed before
  `resolve_stale` runs. A call-site ordering test verifies this.
- [ ] AC6: Inline comments limited to lines that appear in `parse_diff_new_lines()` output (reuse the
  eligibility helper from `ai_pr_review/diff/eligibility.py`). Lines outside the diff window fall
  back to a PR-level review comment with a file reference.
- [ ] AC7: Body-level truncation: if the summary body exceeds GitHub's 65,536-char limit, truncate
  with a clear "(truncated)" marker, same as the bash `truncate_body`.
- [ ] AC8: Skip comments: `post_skip_comment(reason)` posts an issue comment when the Python engine
  runs in no-op mode (e.g., diff empty). Marker-embedded so subsequent runs can detect/replace.
- [ ] AC9: All HTTP calls tolerate a mockable transport (dependency-injected `http.Client` or
  `httpx.AsyncClient`) — no direct `subprocess.run("gh", ...)` calls.
- [ ] AC10: `mypy --strict` and `ruff check` clean.
- [ ] AC11: Unit tests cover: summary upsert happy path, summary upsert with existing marker (sha
  replacement), findings post with inline-eligible + inline-ineligible mix, stale thread resolution
  limited to markered comments, retry on transient 502/503/429, truncation, skip comment.
- [ ] AC12: Integration-style test using recorded tapes from the Epic 0 harness — GitHub fixture
  flows through the Python GitHub provider and produces byte-identical API request payloads (within
  tolerances documented in `tests/golden/tolerances.md`).

## Tasks/Subtasks

- [ ] T1: Create `ai_pr_review/vcs/protocol.py`
  - [ ] T1.1: Define `VcsProvider` Protocol + result dataclasses (`SummaryResult`, `FindingsResult`,
        `StaleResult`, `Finding`, `DiffContext`) in typed form
  - [ ] T1.2: Export common enums/types that all providers share (e.g., `PostOutcome`)
- [ ] T2: Create `ai_pr_review/vcs/http.py` (shared HTTP helper)
  - [ ] T2.1: Retry wrapper (`retry_transient(func, attempts=3, backoff=2.0, jitter=True)`)
  - [ ] T2.2: Tape recorder (reads `AI_PR_REVIEW_RECORD_DIR`; writes tape files matching Epic 0 schema)
  - [ ] T2.3: Secret redaction on tapes (port `_redact_secrets` semantics)
- [ ] T3: Create `ai_pr_review/vcs/github.py`
  - [ ] T3.1: `GitHubProvider` class implementing `VcsProvider`
  - [ ] T3.2: `get_last_reviewed_sha` — scan summary comment history
  - [ ] T3.3: `post_summary` — upsert by marker (create-or-replace-sha)
  - [ ] T3.4: `post_findings` — create review, add inline comments gated by diff eligibility
  - [ ] T3.5: `resolve_stale` — GraphQL threads + REST reviews, both marker-gated
  - [ ] T3.6: `post_skip_comment` + marker round-trip
  - [ ] T3.7: Truncation helper (shared via `ai_pr_review/vcs/_body.py` if reused later)
- [ ] T4: Wire into CLI dispatch
  - [ ] T4.1: `ai_pr_review/cli.py` grows a `post` subcommand that selects provider by
        `VCS_PROVIDER` env + falls through to the current shim path when missing
  - [ ] T4.2: Protocol-compliant provider selection helper in `ai_pr_review/vcs/__init__.py`
- [ ] T5: Tests
  - [ ] T5.1: `tests/python/vcs/test_protocol.py` — Protocol conformance tests (structural)
  - [ ] T5.2: `tests/python/vcs/test_github_provider.py` — unit tests with httpx mock transport
  - [ ] T5.3: Retry behavior (three transient failures then success, three failures then raise)
  - [ ] T5.4: Marker-gated stale cleanup: non-markered comments MUST NOT be resolved
  - [ ] T5.5: Ordering test: cleanup runs after successful post (use call-order fake)
  - [ ] T5.6: Golden-tape integration test for the GitHub path
- [ ] T6: Run mypy + ruff + pytest; confirm clean

## Dev Notes

### Bash references

- `post-review.sh:55` — `gh_api_retry` (retry policy, tape recording hook)
- `post-review.sh:380` — `get_last_reviewed_sha` (summary-comment scan for watermark)
- `post-review.sh:401` — `resolve_stale_threads` (GraphQL; currently filters by author only —
  #183)
- `post-review.sh:497` — `dismiss_stale_reviews` (REST review dismissal)
- `post-review.sh:598` — `post_summary`
- `post-review.sh:687` — `post_findings`
- `post-review.sh:1154` — `update_sha_marker`
- `vcs/common.sh:235` — `format_body_finding` (already partially ported into E2.S6/S8;
  reuse/extend here)

### Why a Protocol (not an ABC)?

Structural typing lets S10/S11 providers plug in without inheritance, and unit tests can supply
fakes without subclassing ceremony. All three providers share **behavior contracts**, not shared
implementation — the HTTP helper in T2 is the only code reuse.

### Previous learnings from E2.S1–S8

- `from __future__ import annotations`
- Module-level constants use `Final`
- Async boundary: providers can be async (anyio) — S9 chooses *sync* for now since posting isn't on
  the hot path and simpler to reason about. Revisit if profiling shows serial posting dominates.
- `mypy --strict` for everything in `ai_pr_review/vcs/`

## File List

- `ai_pr_review/vcs/protocol.py` (new)
- `ai_pr_review/vcs/github.py` (new)
- `ai_pr_review/vcs/http.py` (new)
- `ai_pr_review/vcs/__init__.py` (modified — export protocol + provider factory)
- `ai_pr_review/cli.py` (modified — optional `post` subcommand)
- `tests/python/vcs/test_protocol.py` (new)
- `tests/python/vcs/test_github_provider.py` (new)
- `tests/python/vcs/test_http.py` (new)
- `memory-bank/bmad/stories/2-9-vcs-github.md` (this file)

## Change Log

- 2026-05-13: Created E2.S9 story — VCS protocol + GitHub provider.
