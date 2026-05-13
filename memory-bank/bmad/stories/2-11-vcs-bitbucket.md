---
title: "E2.S11 — Bitbucket provider"
epic: 2
story: 11
status: blocked-by-s9
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

- [ ] AC1: `BitbucketProvider(workspace: str, repo_slug: str, token: str, pr_id: int, http: Client)`
  implements `VcsProvider`.
- [ ] AC2: `resolve_repo_id` resolves Bitbucket repo numeric/slug identity; cached.
- [ ] AC3: `post_summary` + `post_findings` collapse into a single "summary comment containing
  findings" call (Bitbucket Cloud's current model). Upsert keyed by `SUMMARY_MARKER_PREFIX`.
- [ ] AC4: `resolve_stale` deletes/hides previous summary comments that contain `INLINE_MARKER` or
  `SUMMARY_MARKER_PREFIX` from this bot only. Non-markered comments from other bots are left
  alone.
- [ ] AC5: `post_skip_comment` posts a no-op PR comment.
- [ ] AC6: SHA watermark advance on the existing summary comment.
- [ ] AC7: Retry policy + tape recording match the GitHub/GitLab providers.
- [ ] AC8: Truncation to Bitbucket's comment length limit.
- [ ] AC9: `mypy --strict` and `ruff check` clean; Protocol conformance test from S9 passes.
- [ ] AC10: Unit tests cover: summary upsert, replacement of existing summary, marker-gated
  cleanup, retry, truncation, skip.
- [ ] AC11: Integration-style test against recorded Bitbucket tapes.

## Tasks/Subtasks

- [ ] T1: Scaffold `ai_pr_review/vcs/bitbucket.py` with `BitbucketProvider`.
- [ ] T2: Port `resolve_repo_id`, `bb_api`, `render_findings_markdown`, `build_comment_body`.
- [ ] T3: Port `find_existing_summary_id`, `post_summary_with_findings`.
- [ ] T4: Port `_cleanup_duplicate_summary_comments` with marker gate.
- [ ] T5: Port `update_sha_marker`.
- [ ] T6: Tests in `tests/python/vcs/test_bitbucket_provider.py`.
- [ ] T7: Register in factory.
- [ ] T8: mypy + ruff + pytest clean.

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

- `ai_pr_review/vcs/bitbucket.py` (new)
- `tests/python/vcs/test_bitbucket_provider.py` (new)
- `ai_pr_review/vcs/__init__.py` (modified — register `BitbucketProvider`)
- `memory-bank/bmad/stories/2-11-vcs-bitbucket.md` (this file)

## Change Log

- 2026-05-13: Created E2.S11 story — Bitbucket provider.
