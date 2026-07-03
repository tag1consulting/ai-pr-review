# Story 13.5: verify review state before dismiss PUT (shared helper)

Status: review

PR: TBD

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a **maintainer of ai-pr-review**,
I want the shared `_dismiss_if_all_resolved()` helper to check a target review's `state` before issuing a dismiss PUT,
so that resolving the last unresolved thread on a review that is already `DISMISSED`/`APPROVED`/`COMMENTED` is a clean no-op instead of a wasted API call that GitHub correctly rejects, matching the guard the original bash job had (`if review_state != CHANGES_REQUESTED: skip`).

This is a follow-up story to Epic 13 (GitHub issue #554), filed as issue #562 during story 13-3's implementation and review. It is not one of the epic's original 4 phases; it fixes a real, live-verified gap found in the shared helper both `dismiss_by_finding_id` (13-2) and `dismiss_inline_reply` (13-3) call.

## Acceptance Criteria

1. `_dismiss_if_all_resolved()` in `ai_pr_review/slash/dismiss.py` fetches (or is given) the target review's current `state` and skips the dismiss PUT entirely — returning `(False, errors)` with no HTTP call to `dismiss_review` — when `state != "CHANGES_REQUESTED"`. This must be a genuine skip (no PUT attempted at all), not merely surfacing the resulting 422 as a warning (that's what story 13-3 already does; this story prevents the wasted call in the first place).
2. The fix lives at the shared-helper level, not duplicated per call site — both `dismiss_by_finding_id` (story 13-2, already merged) and `dismiss_inline_reply` (story 13-3, already merged) get the fix automatically since both call `_dismiss_if_all_resolved()`.
3. A new `GitHubProvider.get_review_state(review_id) -> str | None` method (or equivalent) fetches a single review's `state` via `GET /repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}` — a dedicated single-review GET, not a re-list of all reviews via `list_bot_reviews()` (that method paginates the full list and filters to bot-authored reviews, which is unnecessary and wasteful for checking one already-known review id). Returns `None` on any HTTP error (appended to `self._errors`, matching the existing error-handling convention in this file), which `_dismiss_if_all_resolved()` must treat as "could not verify state" — **fail closed (skip the dismiss), not fail open** — since a wasted API call is the failure mode being eliminated, not traded for a wrongful dismiss attempt on unverifiable state.
4. `DismissResult.errors` continues to surface any state-fetch failure as a warning (consistent with story 13-3's existing error-surfacing fix), distinct from the (now silent, expected) "not CHANGES_REQUESTED, skipping" case — the latter is not an error, it's the correct behavior, and must not be logged as one.
5. No change to `dismiss_review()`'s own behavior (the PUT primitive itself) — this story only adds a check *before* calling it, inside `_dismiss_if_all_resolved()`.
6. No change to the "which review to target" or "count scope" semantics established in story 13-1/13-2/13-3 (per-review, never PR-wide) — this story is additive, not a redesign.
7. New test coverage in `tests/python/vcs/test_dismiss_github.py` (HTTP-mocked, following the existing `_make_provider(handler)` harness): a target review with `state == "DISMISSED"` and zero unresolved threads must resolve the thread but skip the dismiss PUT entirely (assert no `PUT .../dismissals` call is made, `review_dismissed is False`, no error); a target review with `state == "CHANGES_REQUESTED"` and zero unresolved threads must still dismiss correctly (regression guard — the fix must not break the existing happy path, already covered by `test_dismiss_by_finding_id_inline_resolves_and_dismisses_review`, but re-verify it still passes); a state-fetch HTTP failure (e.g. 404) must fail closed (no PUT, error surfaced) rather than silently proceeding to dismiss anyway.
8. New unit test for `GitHubProvider.get_review_state()` covering the happy path and the HTTP-error path.
9. `pytest tests/python -q`, `mypy ai_pr_review/`, and `ruff check ai_pr_review/ tests/python/` all pass clean. No regression in any existing dismiss test file (`test_dismiss.py`, `test_dismiss_github.py`, `test_cli_dismiss.py`, `test_cli_dismiss_inline.py`, `test_cli_feedback_context.py`).
10. **Live verification against `tag1consulting/ai-pr-review-test`, per issue #562's own stated verification criteria**, scope permitting: confirm resolving the last unresolved thread on a real `CHANGES_REQUESTED` review still dismisses it correctly, and confirm resolving the last unresolved thread on a non-`CHANGES_REQUESTED` review (e.g. the already-`DISMISSED` review `4284199739` on `tag1consulting/ai-pr-review-test#1`, which has 25 unresolved threads, cited in issue #562) is now a clean no-op with no PUT attempted and no warning. If this can't be safely arranged live within this story (e.g. it risks disturbing the shared test repo's fixture state for other in-flight work), defer explicitly to the epic's already-tracked combined `:dev` pre-tag live-e2e pass rather than silently marking this AC done.

## Tasks / Subtasks

- [x] Task 1 — `GitHubProvider.get_review_state()` (AC: 3, 8)
  - [x] Add a `_review_url(review_id)` path helper for `GET /repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}`
  - [x] Add `get_review_state(review_id: int) -> str | None`, returning the review's `state` field or `None` on any HTTP error (appended to `self._errors`)
  - [x] Unit test: happy path (returns the state string) and HTTP-error path (returns `None`, error appended)
- [x] Task 2 — Wire the check into `_dismiss_if_all_resolved()` (AC: 1, 2, 4, 5, 6)
  - [x] Fetch the target review's state before issuing the dismiss PUT (after the unresolved-count check passes, since there's no reason to spend an API call verifying state on a review that still has unresolved threads)
  - [x] Skip the dismiss (return `False`, no error) when the state is fetched successfully but isn't `CHANGES_REQUESTED`
  - [x] Fail closed (skip the dismiss, surface an error) when the state fetch itself fails
  - [x] Do not change any other behavior in this function
- [x] Task 3 — Tests (AC: 7, 9)
  - [x] New tests in `tests/python/vcs/test_dismiss_github.py`: non-`CHANGES_REQUESTED` state skips cleanly, `CHANGES_REQUESTED` still dismisses (regression), state-fetch failure fails closed
  - [x] Full-suite regression pass: `pytest tests/python -q` (1762 passed), `mypy ai_pr_review/` (clean), `ruff check ai_pr_review/ tests/python/` (clean)
  - [x] Background code-review pass — no findings at reporting threshold; confirmed the fail-closed/silent-skip distinction, check ordering, single-vs-list endpoint separation, and mock-handler correctness are all right
  - [x] Updated 4 pre-existing tests' mock HTTP handlers (in `test_dismiss_github.py` and `test_cli_dismiss_inline.py`) to stub the new single-review GET endpoint, since the state-check code path now runs during their execution
- [ ] Task 4 — Live verification (AC: 10, deferred — see Debug Log)

## Dev Notes

- **This story exists because of a gap found and explicitly deferred during story 13-3**, not a new discovery. See `memory-bank/bmad/implementation-artifacts/13-3-wire-dismiss-finding.md`'s Completion Notes for the original finding: `_dismiss_if_all_resolved()` never checked review state before dismissing, unlike the bash job it replaced. Confirmed live-reachable at the time: review `4284199739` on `tag1consulting/ai-pr-review-test#1` was already `DISMISSED` with 25 unresolved threads. Story 13-3 fixed the *silent-failure* half of this (the resulting 422 now surfaces as a `::warning::` instead of being swallowed) but deliberately left the *wasted-API-call* half — the actual state check — for a follow-up, filed as issue #562, specifically because the fix belongs at the shared-helper level (`_dismiss_if_all_resolved()`) and should be verified once rather than patched twice across the two call sites (13-2's `dismiss_by_finding_id`, 13-3's `dismiss_inline_reply`).
- **Where to get the review state without an extra unnecessary round-trip.** Both call sites currently discard any review-list data they fetch before reaching `_dismiss_if_all_resolved()` — `dismiss_by_finding_id` calls `provider.list_bot_reviews()` early (for F-id classification), but that list is built and consumed before thread resolution happens, and threading it through to the dismiss step would entangle two independently-reasoned phases of the function for a small savings. `dismiss_inline_reply` doesn't fetch any review list at all today. The simplest, most contained fix — checked with `list_bot_reviews()`'s and `dismiss_review()`'s existing method shapes in `ai_pr_review/vcs/github.py` — is a new single-review GET (`get_review_state`), called only when `_dismiss_if_all_resolved()` is actually about to dismiss (i.e., only when the unresolved-count check has already passed), so the common "some threads still unresolved" path never pays for the extra call.
- **Fail-closed, not fail-open, on a state-fetch error.** If `get_review_state()` returns `None` (any HTTP error), the correct behavior is to skip the dismiss and surface an error — not to proceed with the PUT as if state were unknown-but-fine, and not to silently succeed as if nothing needed doing. This mirrors the existing #555-class discipline already established in this module (`context_from_parent_comment`'s fetch-failure handling, `resolve_only`'s GraphQL-200-with-errors handling): an inability to verify a precondition is itself reported, never silently assumed in either direction.
- **This story was scoped via `sprint-status.yaml` and hand-authored**, not run through the full `bmad-create-story` skill's epics/PRD/architecture discovery pipeline — Epic 13 was never added to `memory-bank/bmad/planning-artifacts/epics-and-stories.md` (it was scoped directly from an approved implementation plan file during story 13-1), so that skill's step 2 (extract from `{epics_content}`) has no matching source material. This continues the same hand-authored-story convention already used for stories 13-1 through 13-4.

### Project Structure Notes

- Modified: `ai_pr_review/vcs/github.py` (new `get_review_state()`, plus a path helper).
- Modified: `ai_pr_review/slash/dismiss.py` (`_dismiss_if_all_resolved()` gains the state check; no signature change expected for `dismiss_by_finding_id`/`dismiss_inline_reply`, since this is internal to the shared helper).
- Modified: `tests/python/vcs/test_dismiss_github.py` (new tests for the state-check behavior and `get_review_state()`).
- No changes expected to `ai_pr_review/cli.py` or any workflow YAML — this is a pure logic fix inside the already-wired dismiss/dismiss-inline CLI paths, not a new integration point.

### References

- [Source: https://github.com/tag1consulting/ai-pr-review/issues/562] — the issue this story implements; states the problem, fix approach, and verification criteria directly
- [Source: memory-bank/bmad/implementation-artifacts/13-3-wire-dismiss-finding.md, Completion Notes] — where this gap was originally found and deliberately deferred
- [Source: ai_pr_review/slash/dismiss.py:235-283] — `_dismiss_if_all_resolved()`, the function this story modifies
- [Source: ai_pr_review/vcs/github.py:719-734] — `dismiss_review()`, the PUT primitive this story adds a precondition check in front of (unmodified by this story)
- [Source: ai_pr_review/vcs/github.py:758+] — `list_bot_reviews()`, an existing paginated-list primitive deliberately NOT reused for this story's single-review lookup (see Dev Notes)

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- **Test-mock fallout, expected and fixed, not a design surprise:** adding the state-check call site surfaced 4 pre-existing tests whose mock HTTP handlers matched on a broad `"/reviews" in url` substring, which intercepted the new single-review GET (`.../reviews/{id}`) and routed it to their list-shaped (`list[dict]`) response, causing an `AttributeError: 'list' object has no attribute 'get'` inside `get_review_state()`. Fixed by adding a more specific `url.endswith("/reviews/{id}")` branch checked before the broader substring match in each affected handler (`test_dismiss_by_finding_id_inline_resolves_and_dismisses_review` in `test_dismiss_github.py`; `test_resolves_thread_and_dismisses_review_when_review_id_given`, `test_dismiss_put_failure_surfaces_as_warning_not_silent`, `test_missing_review_id_falls_back_to_thread_review_and_still_resolves` in `test_cli_dismiss_inline.py`). No production-code ambiguity exists between the two endpoints (confirmed by the background code-review pass) — this was purely a test-mock specificity gap.
- **Live verification (AC 10) deferred, not attempted.** Mutating review state on the shared `tag1consulting/ai-pr-review-test` repo (dismissing/resolving real threads) carries a real risk of disturbing fixture state other in-flight work depends on, and the fix is small, fully covered by HTTP-mocked tests, and reviewed. Deferred explicitly to the epic's already-tracked combined `:dev` pre-tag live-e2e pass (per stories 13-2/13-3/13-4's own precedent for the same class of deferral) rather than attempted mid-session.

### Completion Notes List

- No deviations from the story's acceptance criteria. The fix is additive and contained entirely within `_dismiss_if_all_resolved()` and a new `GitHubProvider.get_review_state()` method — no signature changes to `dismiss_by_finding_id` or `dismiss_inline_reply`, no CLI changes, no workflow YAML changes.
- **Fail-closed on state-fetch failure, silent-skip on known-wrong state — verified as two genuinely distinct code paths**, not conflated, by both self-review and the background code-review agent: a `None` return from `get_review_state()` (any HTTP error) appends an error and skips; a successfully-fetched non-`CHANGES_REQUESTED` state skips with no error (this is the correct, expected, silent behavior the original bash job also had).
- Background code-review pass (dispatched before opening the PR) reported no findings at its reporting threshold. It independently verified: the fail-closed/silent-skip distinction, the check ordering (state fetch only happens after the unresolved-count check passes, so PR runs with unresolved threads never pay for the extra API call), no single-review-vs-list-endpoint confusion in production code, and that all 4 modified pre-existing tests' original assertions remain intact.
- Task 4 (live verification) deferred — see Debug Log Reference above.

### File List

- `ai_pr_review/vcs/github.py` — new `_review_url()` path helper; new `get_review_state()` method
- `ai_pr_review/slash/dismiss.py` — `_dismiss_if_all_resolved()` gains the review-state check before its dismiss PUT
- `tests/python/vcs/test_dismiss_github.py` — new tests for the skip-on-wrong-state case, fail-closed-on-fetch-error case, and `get_review_state()` (happy path + HTTP error); updated one existing test's mock handler
- `tests/python/test_cli_dismiss_inline.py` — updated 3 existing tests' mock handlers to stub the new single-review GET endpoint
- `memory-bank/bmad/implementation-artifacts/sprint-status.yaml` — Epic 13 reopened to `in-progress`; story 13-5 added
