---
title: "E2.S9 — VCS protocol + GitHub provider"
epic: 2
story: 9
status: review
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

- [x] AC1: `ai_pr_review/vcs/protocol.py` defines a `VcsProvider` `typing.Protocol` with:
  - `post_summary(summary_body: str, head_sha: str) -> SummaryResult`
  - `post_findings(findings: list[Finding], diff: DiffContext, head_sha: str) -> FindingsResult`
  - `resolve_stale(head_sha: str) -> StaleResult` (marker-gated)
  - `get_last_reviewed_sha(pr_number: int) -> str | None`
  - `post_skip_comment(reason: str) -> None`
  - Typed return dataclasses carry posted/skipped/error counts and optional error messages.
- [x] AC2: `ai_pr_review/vcs/github.py` implements `VcsProvider` for GitHub REST + GraphQL:
  - Summary comment upsert (keyed by `SUMMARY_MARKER_PREFIX` — one per PR).
  - Findings posted as a PR review with inline comments, each containing `INLINE_MARKER`.
  - Stale thread resolution via GraphQL `resolveReviewThread`, filtered by `has_inline_marker(body)`
    AND author match (defense in depth).
  - Stale review dismissal for old reviews from this bot, again gated by marker.
  - SHA watermark advance via `replace_summary_sha` on the existing summary comment (2.FR-7 semantics
    already encoded in E2.S7 — this story *consumes* that policy).
- [x] AC3: `gh_api_retry` equivalent: retry 502/503/429/504 + timeouts up to 3× with exponential
  backoff + jitter. Unit tests with stubbed transport.
- [x] AC4: VCS tape recording honored when `AI_PR_REVIEW_RECORD_DIR` is set (method, URL, request
  body, response body, sequence number; secrets redacted before disk write).
- [x] AC5: Cleanup order invariant — `post_summary` and `post_findings` MUST succeed before
  `resolve_stale` runs. Verified via `test_github_ordering.py`.
- [x] AC6: Inline comments limited to lines in `parse_added_lines()`; context lines still eligible
  as start_line for multi-line suggestions via `parse_new_file_lines()`. Ineligible lines fall back
  to the review body with a "line not in diff" note.
- [x] AC7: Body truncation at 65,536 bytes, UTF-8 safe (drops partial trailing codepoint), append
  clear "truncated" marker.
- [x] AC8: `post_skip_comment(reason)` posts an issue comment bearing `INLINE_MARKER` with a default
  "No changes to review." when reason is empty.
- [x] AC9: All HTTP calls go through an injected `httpx.Client`; tests drive `httpx.MockTransport`
  — zero `subprocess.run("gh", ...)` anywhere.
- [x] AC10: `mypy --strict` and `ruff check` clean across `ai_pr_review/` and `tests/python/`.
- [x] AC11: 93 unit tests cover: summary upsert happy path + existing-marker replacement + duplicate
  cleanup + empty-body refusal + API error, inline-eligible/ineligible mix, max-inline cap,
  triple-backtick suggestion rejection, APPROVE split into pre-COMMENT + APPROVE, REQUEST_CHANGES
  retry-as-COMMENT fallback, full issue-comment fallback, marker-gated stale resolution (other
  bots, spoofed marker, already-resolved), stale dismissal respect for pending markered threads,
  retry (success, recovery, exhaustion, 4xx pass-through, network error, non-transient exception),
  UTF-8-safe truncation, tape recording with redaction.
- [ ] AC12 (DEFERRED to S13): Golden-tape integration test. Tapes for GitHub will be recorded via
  `AI_PR_REVIEW_RECORD_DIR` against `~/repos/tag1/ai-pr-review-test-github` during S13 fixture work,
  then wired into `tests/golden/diff_harness.py`. Scope-deferred by user for this PR.

## Tasks/Subtasks

- [x] T1: Create `ai_pr_review/vcs/protocol.py`
  - [x] T1.1: `VcsProvider` Protocol + `SummaryResult`, `FindingsResult`, `StaleResult`,
        `DiffContext`, `PostEvent`
  - [x] T1.2: Reused `Finding` from `ai_pr_review.findings.models` rather than duplicating
- [x] T2: Create `ai_pr_review/vcs/http.py`
  - [x] T2.1: `retry_transient(func, policy)` with configurable attempts/backoff/jitter/sleep
  - [x] T2.2: `TapeRecorder` honoring `AI_PR_REVIEW_RECORD_DIR`
  - [x] T2.3: `redact_secrets` — scrubs Authorization/Private-Token headers and `ghp_`, `glpat-`,
        `sk-` value patterns before disk write
- [x] T3: Create `ai_pr_review/vcs/github.py`
  - [x] T3.1: `GitHubProvider` implements `VcsProvider` (verified via `runtime_checkable` isinstance)
  - [x] T3.2: `get_last_reviewed_sha` — paginated summary-comment scan + `extract_summary_sha`
  - [x] T3.3: `post_summary` — upsert by marker + `advance_sha_watermark` + duplicate cleanup
  - [x] T3.4: `post_findings` — diff-eligible inline anchoring, suggestion validation (range limit,
        triple-backtick rejection, multi-line context check), APPROVE split, retry-as-COMMENT,
        final issue-comment fallback
  - [x] T3.5: `resolve_stale` — marker-gated GraphQL + REST dismissal (closes #183, #184)
  - [x] T3.6: `post_skip_comment` adds `INLINE_MARKER`
  - [x] T3.7: `ai_pr_review/vcs/_body.py` — `severity_icon`, `format_source_tag`,
        `format_body_finding`, `truncate_body`, `build_agent_prompt`
- [ ] T4 (DEFERRED to S12): CLI wiring. The `review` subcommand belongs in S12 alongside the
  engine-switch shim removal; wiring it up now would require a partial `AI_PR_REVIEW_ENGINE=python`
  path that S12 will replace. Deferring avoids churn.
- [x] T5: Tests (93 passing; see File List below)
  - [x] T5.1: `test_protocol.py` — structural Protocol conformance
  - [x] T5.2: `test_github_summary.py` (13), `test_github_findings.py` (8), `test_github_stale.py` (6)
  - [x] T5.3: `test_http.py` retry coverage (6 tests)
  - [x] T5.4: Marker-gated stale cleanup — non-markered + spoofed-marker + other-bot author
  - [x] T5.5: `test_github_ordering.py` — post_summary → post_findings → resolve_stale sequence
  - [ ] T5.6 (DEFERRED to S13): Golden-tape integration
- [x] T6: mypy --strict clean (42 source files); ruff clean; pytest 577 passed

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

### Added
- `ai_pr_review/vcs/protocol.py` — VcsProvider Protocol + result dataclasses
- `ai_pr_review/vcs/http.py` — retry, tape recorder, secret redaction, RecordingClient
- `ai_pr_review/vcs/_body.py` — body formatting helpers (severity icon, source tag,
  body finding rendering, UTF-8-safe truncation, agent-prompt block)
- `ai_pr_review/vcs/github.py` — GitHubProvider (port of post-review.sh)
- `tests/python/vcs/test_http.py` — 16 tests (retry, redaction, tape recording)
- `tests/python/vcs/test_body.py` — 12 tests
- `tests/python/vcs/test_protocol.py` — 1 structural conformance test
- `tests/python/vcs/test_github_summary.py` — 13 tests (summary upsert, skip, last-SHA, watermark)
- `tests/python/vcs/test_github_findings.py` — 8 tests (inline anchoring, fallbacks, suggestions)
- `tests/python/vcs/test_github_stale.py` — 6 tests (marker gating, dismissal)
- `tests/python/vcs/test_github_ordering.py` — 1 test (post_summary → findings → resolve_stale)

### Deferred (intentionally — see T4/AC12/T5.6)
- `ai_pr_review/cli.py` — CLI `review` subcommand wiring belongs in S12 (shim removal)
- `ai_pr_review/vcs/__init__.py` provider factory — same

## Dev Agent Record

### Implementation Plan

1. Protocol + result types first (contract).
2. Shared HTTP layer (retry + tape + redaction) — reusable by S10/S11.
3. Body formatting helpers — reusable.
4. GitHubProvider in layers: paths → last-SHA → summary upsert → skip → watermark → findings → stale.
5. Tests alongside each module; re-verify full vcs suite + mypy + ruff after each layer.

### Completion Notes

- Retry classification: 502/503/504/429 + `httpx.TimeoutException | NetworkError` count as
  transient. 500 does NOT — bash precedent showed 500s were usually non-transient application
  errors worth surfacing.
- Marker-gating is a **hard** filter (AC2/AC5 for stale). Defense-in-depth: even with our marker
  present, if the author login doesn't match our bot, we skip. This closes the copy-paste spoof
  scenario (another bot copying our marker string).
- APPROVE reviews with inline comments: GitHub rejects inline anchors on APPROVE, so the provider
  emits two API calls — a COMMENT review carrying the inline anchors, then an APPROVE body-only.
  Same behavior as bash; tested in `test_post_findings_approve_with_inline_splits_into_two_posts`.
- Fallback chain on review post failure: REQUEST_CHANGES/APPROVE → COMMENT → plain issue comment
  with rendered inline bodies. All three legs exercised by tests.
- `advance_sha_watermark` is a separate method (not inside `post_summary`) so the orchestrator can
  choose per-push semantics (advance on success only) — this is what E2.S7's watermark policy
  consumes.
- Deferred scope: CLI wiring (T4) and golden-tape integration (AC12/T5.6) — see the deferred
  entries above. Deferrals discussed with Greg; both deferrals sit naturally inside S12 and S13.

### Test results

- `pytest tests/python/vcs/` — **93 passed**
- `pytest` (full repo) — **577 passed**
- `mypy --strict ai_pr_review/` — no issues (42 source files)
- `ruff check ai_pr_review/ tests/python/` — all checks passed

## Change Log

- 2026-05-13: Created E2.S9 story — VCS protocol + GitHub provider.
- 2026-05-13: Implementation complete for T1-T3, T5, T6. T4 + AC12/T5.6 deferred to S12/S13 by
  agreement. 93 new vcs tests; full repo test count 577. mypy strict + ruff clean. Status → review.
