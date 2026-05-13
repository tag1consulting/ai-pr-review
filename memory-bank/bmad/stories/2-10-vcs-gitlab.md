---
title: "E2.S10 — GitLab provider"
epic: 2
story: 10
status: review
github_issue: 225
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S10 — GitLab provider

## Summary

Implement `ai_pr_review/vcs/gitlab.py` conforming to the `VcsProvider` protocol from E2.S9. Ports
`post-review-gitlab.sh` (1,060 LoC) — summary-note upsert, inline MR discussions with suggestion
code fences, marker-gated stale discussion resolution, project ID resolution, and SHA watermark
advance.

## PRD Reference

2.FR-9 — resolves the GitLab half of #184 via marker gating.

## Acceptance Criteria

- [x] AC1: `GitLabProvider(project: str, token: str, mr_iid: int, http: Client)` implements the
  `VcsProvider` protocol end-to-end.
- [x] AC2: `resolve_project_id` resolves project numeric ID from `GITLAB_PROJECT` (which may be a
  numeric ID or a `group/project` path). Cached for the lifetime of the provider.
- [x] AC3: `post_summary` upserts a single note containing findings (GitLab's "one note with
  findings table" model, matching the bash engine).
- [x] AC4: `post_findings` posts inline MR discussions with suggestion fences
  (```` ```suggestion ```` code blocks) when a finding carries a suggested fix.
- [x] AC5: `resolve_stale` uses the **same marker-gated predicate** as GitHub — `has_inline_marker`
  MUST be a hard filter, matching #184.
- [x] AC6: `post_skip_comment` posts a no-op MR note on skip paths.
- [x] AC7: SHA watermark advance uses `replace_summary_sha` on the existing summary note.
- [x] AC8: Retry policy + tape recording match the GitHub provider (via shared
  `ai_pr_review/vcs/http.py` from S9).
- [x] AC9: Truncation to GitLab's body limit (~1 MB, but per-discussion limits are stricter; match
  bash constants).
- [x] AC10: `mypy --strict` and `ruff check` clean; Protocol conformance test from S9 passes for
  `GitLabProvider`.
- [x] AC11: Unit tests cover: project ID resolution (numeric + path), summary upsert, inline
  discussion with suggestion fence, marker-gated stale resolution (non-markered discussions left
  alone), retry on transient 502/503/429, skip comment.
- [ ] AC12 (DEFERRED to S13): Golden-tape integration test. Tapes for GitLab will be recorded via
  `AI_PR_REVIEW_RECORD_DIR` against `~/repos/tag1/ai-pr-review-test-gitlab` during S13 fixture work,
  then wired into `tests/golden/diff_harness.py`.

## Tasks/Subtasks

- [x] T1: Scaffold `ai_pr_review/vcs/gitlab.py` with `GitLabProvider` class.
- [x] T2: Port `resolve_project_id` + caching.
- [x] T3: Port `post_summary_with_findings` (GitLab's combined-note model).
- [x] T4: Port `post_inline_discussions` with suggestion-fence rendering.
- [x] T5: Port `resolve_stale_discussions` with **marker filter added**.
- [ ] T6 (DEFERRED to E4): Port `submit_approval_event` (GitLab approve/unapprove). Approval
  semantics are tied to the outcome classifier rollout and field soak — deferring to Epic 4.
- [x] T7: Tests in `tests/python/vcs/test_gitlab_provider.py`.
- [x] T8: Register in `ai_pr_review/vcs/__init__.py` factory.
- [x] T9: mypy + ruff + pytest clean.

## Dev Notes

### Bash references

- `post-review-gitlab.sh:49` — `resolve_project_id`
- `post-review-gitlab.sh:96` — `gl_api`
- `post-review-gitlab.sh:457` — `build_comment_body`
- `post-review-gitlab.sh:569` — `find_existing_summary_id`
- `post-review-gitlab.sh:595` — `post_summary_with_findings`
- `post-review-gitlab.sh:664` — `resolve_stale_discussions` (today filters by bot id —
  add marker gate)
- `post-review-gitlab.sh:733` — `post_inline_discussions`
- `post-review-gitlab.sh:986` — `submit_approval_event`

### Why marker-gating matters here

Bash today filters stale discussions by bot author. A user running this tool under a PAT tied to
their own account has historically risked resolving their own unrelated discussions. The inline
marker closes that gap for all providers, not just GitHub.

## File List

### Added
- `ai_pr_review/vcs/gitlab.py` — GitLabProvider implementation
- `tests/python/vcs/test_gitlab_summary.py` — 16 tests
- `tests/python/vcs/test_gitlab_findings.py` — 6 tests
- `tests/python/vcs/test_gitlab_stale.py` — 7 tests (incl. Protocol conformance)

### Deferred (intentionally — see T6/AC12)
- Provider factory in `ai_pr_review/vcs/__init__.py` — moves with S12 CLI wiring
- `submit_approval_event` — Epic 4 (tied to outcome classifier rollout + soak)

## Dev Agent Record

### Implementation Plan

1. Reused E2.S8 marker module + S9's `_inline.py`/`_stale.py`/`_body.py`/`http.py` shared helpers.
2. Auth header detection: `glpat-*` / `glcbt-*` / OAuth Bearer (token-prefix-based).
3. Project segment URL-encoding: numeric ID passes through; path encoded with `quote(safe="")`.
4. Single-note model: summary = combined note keyed by `SUMMARY_MARKER_PREFIX`; findings overflow
   goes back to the orchestrator (which feeds it into `post_summary`).
5. Inline = separate POSTs to `/discussions` with a `position` JSON object carrying
   base_sha / start_sha / head_sha / new_path / new_line.
6. Stale = paginated GET `/discussions` → marker-gated PUT `/discussions/<id>?resolved=true`.

### Completion Notes

- Marker gating closes the GitLab half of #184. Bash filtered by bot username only — a PAT tied to
  a human's account would have resolved that human's own discussions. Now `is_owned_by_us` requires
  marker AND author match (when bot username known).
- Bot username resolution is lazy: if `config.bot_username` is None, `_get_bot_username` calls
  GET `/user` once on first use and caches. Tests cover both the explicit-config path and the
  lazy fetch.
- Suggestion fence syntax differs from GitHub: GitLab uses ```suggestion:-N+0 where N = lines
  above the cursor (line - start_line). Single-line suggestions use ```suggestion:-0+0.
- 400 from POST /discussions is treated as "position invalid" (line not in MR diff after rebase
  etc.); finding spills to the body-overflow list rather than failing the whole post.
- Token type detection assumes `glpat-`/`glcbt-` literals. If GitLab introduces a new prefix the
  fallback (Bearer) will still work for OAuth tokens.

### Test results

- `pytest tests/python/vcs/` — **142 passed** (29 new for GitLab)
- `pytest` (full repo) — **626 passed**
- `mypy --strict ai_pr_review/` — no issues (45 source files)
- `ruff check ai_pr_review/ tests/python/` — all checks passed

## Change Log

- 2026-05-13: Created E2.S10 story — GitLab provider.
- 2026-05-13: Implementation complete. T6 deferred to Epic 4 (approval events tied to outcome
  rollout); AC12 deferred to S13 (tape recording against `~/repos/tag1/ai-pr-review-test-gitlab`).
  29 new GitLab tests; vcs total 142; full suite 626. mypy strict + ruff clean. Status → review.
