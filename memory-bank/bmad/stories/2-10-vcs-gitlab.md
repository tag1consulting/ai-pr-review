---
title: "E2.S10 — GitLab provider"
epic: 2
story: 10
status: blocked-by-s9
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

- [ ] AC1: `GitLabProvider(project: str, token: str, mr_iid: int, http: Client)` implements the
  `VcsProvider` protocol end-to-end.
- [ ] AC2: `resolve_project_id` resolves project numeric ID from `GITLAB_PROJECT` (which may be a
  numeric ID or a `group/project` path). Cached for the lifetime of the provider.
- [ ] AC3: `post_summary` upserts a single note containing findings (GitLab's "one note with
  findings table" model, matching the bash engine).
- [ ] AC4: `post_findings` posts inline MR discussions with suggestion fences
  (```` ```suggestion ```` code blocks) when a finding carries a suggested fix.
- [ ] AC5: `resolve_stale` uses the **same marker-gated predicate** as GitHub — `has_inline_marker`
  MUST be a hard filter, matching #184.
- [ ] AC6: `post_skip_comment` posts a no-op MR note on skip paths.
- [ ] AC7: SHA watermark advance uses `replace_summary_sha` on the existing summary note.
- [ ] AC8: Retry policy + tape recording match the GitHub provider (via shared
  `ai_pr_review/vcs/http.py` from S9).
- [ ] AC9: Truncation to GitLab's body limit (~1 MB, but per-discussion limits are stricter; match
  bash constants).
- [ ] AC10: `mypy --strict` and `ruff check` clean; Protocol conformance test from S9 passes for
  `GitLabProvider`.
- [ ] AC11: Unit tests cover: project ID resolution (numeric + path), summary upsert, inline
  discussion with suggestion fence, marker-gated stale resolution (non-markered discussions left
  alone), retry on transient 502/503/429, skip comment.
- [ ] AC12: Integration-style test against recorded GitLab tapes from the test repo at
  `gitlab.com/tag1consulting/ai-pr-review-test`.

## Tasks/Subtasks

- [ ] T1: Scaffold `ai_pr_review/vcs/gitlab.py` with `GitLabProvider` class.
- [ ] T2: Port `resolve_project_id` + caching.
- [ ] T3: Port `post_summary_with_findings` (GitLab's combined-note model).
- [ ] T4: Port `post_inline_discussions` with suggestion-fence rendering.
- [ ] T5: Port `resolve_stale_discussions` with **marker filter added**.
- [ ] T6: Port `submit_approval_event` semantics (if kept — else defer to E3/E4).
- [ ] T7: Tests in `tests/python/vcs/test_gitlab_provider.py`.
- [ ] T8: Register in `ai_pr_review/vcs/__init__.py` factory.
- [ ] T9: mypy + ruff + pytest clean.

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

- `ai_pr_review/vcs/gitlab.py` (new)
- `tests/python/vcs/test_gitlab_provider.py` (new)
- `ai_pr_review/vcs/__init__.py` (modified — register `GitLabProvider`)
- `memory-bank/bmad/stories/2-10-vcs-gitlab.md` (this file)

## Change Log

- 2026-05-13: Created E2.S10 story — GitLab provider.
