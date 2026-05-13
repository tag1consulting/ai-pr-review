---
title: "E2.S8 — Ownership marker (marker-gated stale cleanup)"
epic: 2
story: 8
status: ready-for-review
github_issue: 223
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S8 — Ownership marker (marker-gated stale cleanup)

## Summary

Implement `ai_pr_review/vcs/marker.py` — a small module that produces and recognizes the HTML-comment
ownership marker that this tool embeds in every inline comment, review body, and summary comment.
The marker is the precondition for stale-cleanup: S9 (GitHub provider), S10 (GitLab provider), and
the existing Bitbucket path MUST only resolve/dismiss comments whose body contains this marker.
Fixes #183 (GitHub can resolve other bots' reviews) and #184 (GitLab can resolve other bots'
discussions).

Also addresses 2.FR-10: stale cleanup must run **after** a successful post, not before — this story
does not change call ordering (that happens in S9), but the marker module enables the ordering
change by giving cleanup a reliable "owned by us" signal.

## PRD Reference

2.FR-8, 2.FR-10 — resolves #183, #184

## Acceptance Criteria

- [x] AC1: `INLINE_MARKER` constant = `"<!-- ai-pr-review-inline -->"` (single source of truth).
- [x] AC2: `SUMMARY_MARKER_PREFIX` constant = `"<!-- ai-pr-review-summary"` (matches existing bash
  prefix to preserve compatibility with comments posted by the bash engine).
- [x] AC3: `build_summary_marker(head_sha: str) -> str` returns the full summary marker:
  `"<!-- ai-pr-review-summary sha=<head_sha> -->"`. Validates `head_sha` via the same regex as
  S7 (`^[0-9a-f]{7,40}$`); empty/invalid SHA → `"<!-- ai-pr-review-summary -->"` (no `sha=`).
- [x] AC4: `extract_summary_sha(body: str) -> str | None` parses the SHA from a summary marker line.
  Returns None if the marker isn't present, is malformed, or lacks a `sha=` field.
- [x] AC5: `has_inline_marker(body: str) -> bool` returns True iff the body contains `INLINE_MARKER`.
- [x] AC6: `has_summary_marker(body: str) -> bool` returns True iff the body contains the
  `SUMMARY_MARKER_PREFIX` (with or without a `sha=` field).
- [x] AC7: `append_inline_marker(body: str) -> str` appends `INLINE_MARKER` on its own line at the
  end of `body` if not already present; returns body unchanged if marker is already present.
- [x] AC8: `replace_summary_sha(body: str, new_sha: str) -> str` replaces the `sha=...` field in an
  existing summary marker with `new_sha`. If no summary marker exists, returns body unchanged.
  Only replaces within the marker — does not touch the SHA if it happens to appear elsewhere in
  the body.
- [x] AC9: All marker operations are case-sensitive (HTML comments are case-sensitive in practice).
- [x] AC10: `mypy --strict` and `ruff check` clean.
- [x] AC11: Unit tests cover: build/extract round-trip, marker detection with and without
  surrounding text, summary SHA replacement that preserves the rest of the body, malformed markers
  rejected, invalid SHA → marker without sha, idempotent inline marker append, and case-sensitivity.

## Tasks/Subtasks

- [x] T1: Create `ai_pr_review/vcs/` package (if not present) and `ai_pr_review/vcs/marker.py`
  - [x] T1.1: Module-level constants and compiled regexes
  - [x] T1.2: `build_summary_marker`, `extract_summary_sha`, `replace_summary_sha`
  - [x] T1.3: `has_inline_marker`, `has_summary_marker`, `append_inline_marker`
- [x] T2: Write tests in `tests/python/vcs/test_marker.py`
  - [x] T2.1: `build_summary_marker` happy path with valid SHA
  - [x] T2.2: `build_summary_marker` with invalid SHA → no `sha=`
  - [x] T2.3: `extract_summary_sha` happy path
  - [x] T2.4: `extract_summary_sha` from body missing marker → None
  - [x] T2.5: `extract_summary_sha` from marker without `sha=` → None
  - [x] T2.6: `has_inline_marker` / `has_summary_marker` detection
  - [x] T2.7: `has_inline_marker` does NOT match a summary marker (different strings)
  - [x] T2.8: `append_inline_marker` idempotent
  - [x] T2.9: `append_inline_marker` adds newline separator if body doesn't end with one
  - [x] T2.10: `replace_summary_sha` preserves surrounding content
  - [x] T2.11: `replace_summary_sha` is a no-op when no marker present
  - [x] T2.12: Case-sensitivity checks
- [x] T3: Run full test suite + mypy + ruff; confirm clean

## Dev Notes

### Bash reference

From `post-review.sh`:

```bash
MARKER_PREFIX="<!-- ai-pr-review-summary"
# Full marker with SHA: "<!-- ai-pr-review-summary sha=abc1234 -->"
# The existing resolve_stale_threads filters by author login (github-actions[bot]),
# which is precisely what #183 flags as incorrect.
# The Python port introduces INLINE_MARKER and S9 gates cleanup on its presence.
```

### Why two markers?

- **Summary marker**: `<!-- ai-pr-review-summary sha=... -->` — already present in the bash
  engine's summary comment; the sha= field is used by the watermark. We preserve this format for
  compatibility with comments posted by the bash engine during the transition period.
- **Inline marker**: `<!-- ai-pr-review-inline -->` — new. The bash engine does NOT currently put
  any marker on inline comments. The first time a Python-engine review runs on a PR that has
  prior bash-engine inline comments, those un-markered comments will not be treated as stale
  (because they lack our marker). This is the intended conservative behavior per #183 — we'd
  rather leave old findings visible than risk touching another bot's comments.

### Cleanup gating (not this story)

This story **does not** change the cleanup call sites — it only provides the predicate. S9 (GitHub
provider) and S10 (GitLab provider) will add `has_inline_marker(body)` as a mandatory filter
alongside the existing author checks, and will also move cleanup to *after* a successful post
(2.FR-10).

### Previous learnings from E2.S1–S7

- `from __future__ import annotations`
- Module-level constants use `Final` from typing when exported
- Tests: `pytest`
- `mypy --strict`

## File List

- `ai_pr_review/vcs/__init__.py` (new)
- `ai_pr_review/vcs/marker.py` (new)
- `tests/python/vcs/__init__.py` (new)
- `tests/python/vcs/test_marker.py` (new)
- `memory-bank/bmad/stories/2-8-ownership-marker.md` (this file)

## Change Log

- 2026-05-13: Created E2.S8 story — Ownership marker (marker-gated stale cleanup).
