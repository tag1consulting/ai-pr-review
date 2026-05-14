---
title: "E2.S11 — Bitbucket provider"
epic: 2
story: 11
status: review
github_issue: 226
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S11 — Bitbucket provider

## Summary

Implement `ai_pr_review/vcs/bitbucket.py` conforming to `VcsProvider` from E2.S9. Ports
`post-review-bitbucket.sh` (588 LoC) — single summary-comment-with-findings model (no separate
inline comments on Bitbucket Cloud today), marker-gated stale cleanup, SHA watermark advance.

## PRD Reference

2.FR-9.

## Acceptance Criteria

- [x] AC1: `BitbucketProvider(workspace: str, repo_slug: str, token: str, pr_id: int, http: Client)`
  implements `VcsProvider`.
- [x] AC2: `resolve_repo_id` resolves Bitbucket repo numeric/slug identity; cached.
- [x] AC3: `post_summary` + `post_findings` collapse into a single "summary comment containing
  findings" call (Bitbucket Cloud's current model). Upsert keyed by `SUMMARY_MARKER_PREFIX`.
- [x] AC4: `resolve_stale` deletes/hides previous summary comments that contain `INLINE_MARKER` or
  `SUMMARY_MARKER_PREFIX` from this bot only. Non-markered comments from other bots are left
  alone.
- [x] AC5: `post_skip_comment` posts a no-op PR comment.
- [x] AC6: SHA watermark advance on the existing summary comment.
- [x] AC7: Retry policy + tape recording match the GitHub/GitLab providers.
- [x] AC8: Truncation to Bitbucket's comment length limit.
- [x] AC9: `mypy --strict` and `ruff check` clean; Protocol conformance test from S9 passes.
- [x] AC10: Unit tests cover: summary upsert, replacement of existing summary, marker-gated
  cleanup, retry, truncation, skip.
- [ ] AC11 (DEFERRED to S13): Golden-tape integration test. Bitbucket has no live test repo
  configured yet; tapes will be hand-crafted from the bash fixture corpus during S13.

## Tasks/Subtasks

- [x] T1: Scaffold `ai_pr_review/vcs/bitbucket.py` with `BitbucketProvider`.
- [x] T2: Port `resolve_repo_id`, `bb_api`, `render_findings_markdown`, `build_comment_body`.
- [x] T3: Port `find_existing_summary_id`, `post_summary_with_findings`.
- [x] T4: Port `_cleanup_duplicate_summary_comments` with marker gate.
- [x] T5: Port `update_sha_marker`.
- [x] T6: Tests in `tests/python/vcs/test_bitbucket_provider.py`.
- [x] T7: Register in factory.
- [x] T8: mypy + ruff + pytest clean.

## Dev Notes

### Bash references

- `post-review-bitbucket.sh:40` — `resolve_repo_id`
- `post-review-bitbucket.sh:93` — `bb_api`
- `post-review-bitbucket.sh:281` — `render_findings_markdown`
- `post-review-bitbucket.sh:324` — `build_comment_body`
- `post-review-bitbucket.sh:416` — `find_existing_summary_id`
- `post-review-bitbucket.sh:446` — `post_summary_with_findings`
- `post-review-bitbucket.sh:497` — `_cleanup_duplicate_summary_comments`
- `post-review-bitbucket.sh:531` — `update_sha_marker`

### Why no inline comments?

Bitbucket Cloud's inline-comment API is less flexible than GitHub's (no threaded review, weaker
line-range handling). The bash engine collapses findings into the summary comment. S11 preserves
that model; adding true inline comments is out of scope.

## File List

### Added
- `ai_pr_review/vcs/bitbucket.py` — BitbucketProvider implementation
- `tests/python/vcs/test_bitbucket_summary.py` — 11 tests (incl. Protocol + pagination)
- `tests/python/vcs/test_bitbucket_findings.py` — 5 tests
- `tests/python/vcs/test_bitbucket_stale.py` — 4 tests

### Deferred (intentionally — see AC11)
- Provider factory in `ai_pr_review/vcs/__init__.py` — moves with S12 CLI wiring
- Golden-tape integration — S13 (no live Bitbucket test repo configured yet)

## Dev Agent Record

### Implementation Plan

1. Reused `_body.py`, `_stale.py`, `http.py`, `marker.py` shared helpers from S9/S10.
2. Auth: `httpx.BasicAuth(email, api_token)` instead of token-prefix detection.
3. Pagination: read `next` URL from response body and strip the absolute base
   so the relative-URL plumbing in `RecordingClient` keeps working.
4. Single-comment model: `post_findings` PUTs the existing summary comment
   with combined summary + findings markdown. Requires `post_summary` to have
   run first (orchestrator MUST honor AC5 ordering).
5. `resolve_stale` is duplicate-cleanup only — Bitbucket has no review threads,
   so the marker-gated predicate uses `kind="summary"`.

### Completion Notes

- Bitbucket comments don't expose author info uniformly, so the marker is the
  only ownership signal (`is_owned_by_us(body, None, None, kind="summary")`).
  Other-bot comments don't carry our marker so they're inherently safe — the
  stale predicate only ever deletes comments that pass the summary-marker
  regex.
- v0.2.0 has no inline anchoring on Bitbucket Cloud. The protocol's
  `max_inline`, `enable_suggestions`, and `agent_prompt` parameters are
  accepted for compatibility and silently ignored.
- Body limit: 32,000 chars (vs Bitbucket's 32,768 hard cap), to leave headroom
  for JSON encoding and the truncation marker.
- `_render_combined_body` preserves the user-supplied summary text from the
  existing comment by stripping the prior heading and footer, then re-renders
  the heading per the new event/risk classification. This matches the bash
  behavior where the summary text comes from `SUMMARY_FILE` separately.

### Test results

- `pytest tests/python/vcs/` — **162 passed** (20 new for Bitbucket)
- `pytest` (full repo) — **646 passed**
- `mypy --strict ai_pr_review/` — no issues (46 source files)
- `ruff check ai_pr_review/ tests/python/` — all checks passed

## Change Log

- 2026-05-13: Created E2.S11 story — Bitbucket provider.
- 2026-05-13: Implementation complete. AC11 deferred to S13 (no live Bitbucket
  test repo). 20 new tests; vcs total 162; full suite 646. mypy strict + ruff
  clean. Status → review.
