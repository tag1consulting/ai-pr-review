# Story 13.3: `ai-pr-review dismiss-inline` CLI subcommand + wire `dismiss-finding`

Status: review

PR: TBD

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a **maintainer of ai-pr-review**,
I want the `/ai-pr-review dismiss|false-positive|wont-fix` command posted as a reply to an inline review comment handled by a new `ai-pr-review dismiss-inline` CLI subcommand instead of ~270 lines of untested inline bash/GraphQL,
so that the `dismiss-finding` job's thread-resolution and dismissal logic runs the pytest-covered `dismiss_inline_reply` function built in story 13-1, and the bash orchestration this epic exists to retire stops running for the second of its three remaining jobs.

This is phase 3 of a 4-phase epic (Epic 13, GitHub issue #554). Story 13-2 shipped the `ai-pr-review dismiss` CLI subcommand wired to `dismiss_by_finding_id` (the top-level-comment path) and converted the `dismiss-body-finding` job. `dismiss_inline_reply` (the reply-to-inline-comment path) was already implemented and unit-tested in story 13-1 but had zero production call sites. This story adds its CLI entry point and rewires the `dismiss-finding` job. `feedback-command`'s two dismiss-adjacent steps (13-4) remain a separate story.

## Acceptance Criteria

1. A new `@cli.command("dismiss-inline")` is added to `ai_pr_review/cli.py`, alongside the existing `dismiss` subcommand. It reads: `--parent-comment-id` (`envvar=SLASH_IN_REPLY_TO_ID`, required int — the REST `databaseId` of the bot's inline comment being replied to), `--review-id` (`envvar=SLASH_REVIEW_ID`, optional int), `--actor` (`envvar=SLASH_ACTOR`, required), `--command` (`envvar=SLASH_COMMAND`, required, one of `dismiss`/`false-positive`/`wont-fix`), `--pr-number` (`envvar=SLASH_PR_NUMBER`, required int).
2. `--review-id` is passed through to `dismiss_inline_reply(provider, parent_comment_id, review_id, actor=actor, command=command)` **unchanged from whatever the CLI receives** (including `None` when omitted) — it must not be derived, defaulted, or second-guessed at the CLI layer. This matches the bash job's exact behavior: `dismiss-finding`'s "Validate parent comment is from the bot" step (`slash-commands.yml:714-747` pre-change) reads `pull_request_review_id` from the **parent comment** (not the triggering comment) via `gh api repos/.../pulls/comments/{parent_id} --jq '{login, review_id: .pull_request_review_id}'`, and passes that value straight through as `REVIEW_ID` to the dismiss step — with no fallback derivation if it's null (verified: bash's "Dismiss review if all threads resolved" step has no equivalent to `dismiss_inline_reply`'s internal `_thread_review_id(target_thread)` fallback; a missing `review_id` there means the review lookup itself is skipped, not retried against a derived id).
3. The subcommand builds a `GitHubProvider` using the same `_build_github_provider_or_exit()` helper factored out of the `dismiss` command in story 13-2 (do not duplicate the `VCS_PROVIDER` gate + `provider_from_env()` dispatch logic a third time) — GitHub-only, fails closed with a clear message on any other `VCS_PROVIDER`.
4. The subcommand prints `result.reply` to stdout and emits a `::notice::reaction=done|confused` line to stderr, following the exact convention established in story 13-2's `dismiss` command: `done` when `result.thread_resolved or result.review_dismissed`, `confused` otherwise (a genuine miss — thread not found, or found but owned by a different actor).
5. `.github/workflows/slash-commands.yml`'s `dismiss-finding` job (`runs-on: ubuntu-latest`, no `container:` key) is converted to run inside the container, matching the exact pattern used in story 13-2's `dismiss-body-finding` conversion and `feedback-command`'s established block:
   ```yaml
   runs-on: ubuntu-latest
   container:
     image: ghcr.io/tag1consulting/ai-pr-review:${{ inputs.image-tag }}
   defaults:
     run:
       shell: bash
   ```
6. Inside the converted job, the "Find and resolve the review thread" step and the "Dismiss review if all threads resolved" step (two GraphQL-heavy bash blocks, ~230 lines combined) collapse into a single `ai-pr-review dismiss-inline` invocation, following the exact base64-reply-round-trip pattern established in story 13-2's "Invoke Python dismiss handler" step: `reply=$(ai-pr-review dismiss-inline 2>/tmp/dismiss-inline-stderr)`, stderr echoed for error surfacing, exit code checked, reaction marker read from the redirected stderr file (defaulting to `confused` if the marker is never emitted, per story 13-2's inverted-default fix), reply base64-encoded into a step output.
7. The "React — seen" step and the "Validate parent comment is from the bot" step (which resolves `parent_id`/`review_id`/`valid` — this is authentication/authorization logic, not dismiss orchestration, and stays in bash since it's the job's own trust gate) are preserved unchanged.
8. The "React — done", "React — not applicable", and "React — failed" steps are updated to key off the new CLI-invocation step's outcome/reaction-marker output instead of the old bash steps' `outcome`, following story 13-2's "React — dismiss outcome" pattern (a single reaction step driven by the CLI's marker, rather than three separate hardcoded-content steps).
9. The job's trust-gate `if:` condition is preserved unchanged.
10. `tests/python/test_cli_dismiss_inline.py` gains coverage for: thread resolved + review dismissed (review-id given, all other threads already resolved), thread resolved but review-id omitted (falls back to the thread's own review-id per `dismiss_inline_reply`'s documented fallback — not a CLI-layer decision, see AC2), thread not found (confused reaction), thread owned by a different bot/actor (confused reaction, "not posted by this bot" reply), reaction marker on stderr only (never leaking onto stdout — the same base64-corruption risk story 13-2's review caught), non-GitHub `VCS_PROVIDER` failure, missing required option failure.
11. `actionlint .github/workflows/slash-commands.yml` passes clean (no new shellcheck findings beyond the pre-existing style-only ones already present on `main`).
12. `pytest tests/python -q`, `mypy ai_pr_review/`, and `ruff check ai_pr_review/ tests/python/` all pass clean. No regression in `tests/python/slash/test_dismiss.py`, `tests/python/vcs/test_dismiss_github.py` (13-1), or `tests/python/test_cli_dismiss.py` (13-2).
13. **Live-e2e smoke test before merge**, scope permitting: on the seeded `tag1consulting/ai-pr-review-test` repo, exercise the inline dismiss case via the actual "reply to an inline review comment with `/ai-pr-review dismiss`" flow and confirm the converted job produces the same user-facing outcome (thread resolved, review dismissed when applicable, reaction posted) as the bash it replaces. If registry-push permissions block the full YAML-wrapper smoke (as they did in story 13-2), defer that remaining scope to the standard post-merge `:dev` pre-tag e2e gate and document the gap explicitly rather than silently marking this AC fully done.

## Tasks / Subtasks

- [x] Task 1 — `ai-pr-review dismiss-inline` CLI subcommand (AC: 1, 2, 3, 4)
  - [x] Add `@cli.command("dismiss-inline")` in `ai_pr_review/cli.py`, placed after `dismiss`
  - [x] Factor `_build_github_provider_or_exit(command_label)` out of `dismiss`'s inline provider-construction logic; reuse it in both `dismiss` and `dismiss-inline`
  - [x] Wire straight through to `dismiss_inline_reply`, passing `--review-id` unmodified (including `None`)
  - [x] Print `result.reply` to stdout; emit `::notice::reaction=` to stderr per the `thread_resolved`/`review_dismissed` gate
- [x] Task 2 — Convert `dismiss-finding` to a `container:` job (AC: 5, 6, 7, 8, 9)
  - [x] Add the `container:` + `defaults:` block matching `dismiss-body-finding`'s exact syntax
  - [x] Replace "Find and resolve the review thread" + "Dismiss review if all threads resolved" with "Invoke Python dismiss-inline handler" + "Post dismiss reply" + "React — dismiss outcome", following story 13-2's step-collapse pattern exactly
  - [x] Leave "React — seen" and "Validate parent comment is from the bot" untouched — this step already extracts `parent_id`/`review_id`, which map directly onto the new CLI's `--parent-comment-id`/`--review-id` options
  - [x] Leave the job's `if:` trust gate untouched
  - [x] Run `actionlint .github/workflows/slash-commands.yml` — clean (only pre-existing style-only shellcheck notices, confirmed identical to `main` before this change)
- [x] Task 3 — Tests (AC: 10, 12)
  - [x] New `tests/python/test_cli_dismiss_inline.py` following `test_cli_dismiss.py`'s `_make_provider(handler)` harness
  - [x] Cover: resolve+dismiss with explicit review-id, resolve with review-id omitted (thread-derived fallback — verified this is `dismiss_inline_reply`'s existing, already-tested behavior, not new CLI logic), thread not found, other-bot's-thread ignored, reaction marker on stderr only, non-GitHub `VCS_PROVIDER` failure, missing-required-option failure
  - [x] Full-suite regression pass: `pytest tests/python -q` (1726 passed), `mypy ai_pr_review/` (88 files clean), `ruff check ai_pr_review/ tests/python/` (clean)
- [ ] Task 4 — Live-e2e smoke (AC: 13)
  - [ ] Exercise the inline dismiss flow against `tag1consulting/ai-pr-review-test` via a real reply-to-inline-comment; defer YAML-wrapper-only scope to `:dev` pre-tag if registry-push permissions block it again (see story 13-2's precedent)

## Dev Notes

- This story continues Epic 13 (GitHub issue #554); story 13-2's implementation (PR #558, merged at `f3fbabf`) and its companion docs commit (PR #561) are the immediate prior art — this story's CLI-command shape, container-job conversion pattern, and reaction-marker convention all mirror it deliberately, not coincidentally.
- **`dismiss_inline_reply`'s `review_id` parameter already has a fallback, and this story does not change it.** Verified in `ai_pr_review/slash/dismiss.py:431`: `target_review_id = review_id if review_id is not None else _thread_review_id(target_thread)`. A pre-implementation assumption that the CLI needed to prevent this fallback (to strictly match a *literal* reading of the bash job's "no review_id → skip dismissal entirely" behavior) turned out to be based on an unreachable production scenario: every real inline review comment carries a `pull_request_review_id` (spot-checked against 30 real comments on `tag1consulting/ai-pr-review-test#1` and `#2` via `gh api .../pulls/comments --jq '.[] | {review_id: .pull_request_review_id}'` — all non-null), so a parent comment with a null `review_id` and a thread that still resolves to a real review is not a state that occurs in production. The fallback in `dismiss_inline_reply` is pre-existing, already-tested (story 13-1), and this story's CLI simply passes `--review-id` through unmodified rather than reimplementing or suppressing that fallback.
- **The genuinely reachable "no review to target" case** is a parent comment with no `pull_request_review_id` whose thread *also* carries no `pullRequestReview.databaseId` (both facts describe the same underlying reality — the comment's review membership). In that case resolution still succeeds; there's simply nothing to check "all resolved" against, so no dismissal PUT fires. This is the case `test_no_review_anywhere_resolves_without_dismissal` covers.
- **Provider construction is now shared, not reimplemented.** Story 13-2's `dismiss` command had its `VCS_PROVIDER` gate + `provider_from_env()` dispatch inlined directly in the command body. This story factors that into `_build_github_provider_or_exit(command_label: str) -> GitHubProvider` (a module-level helper in `cli.py`), parameterized on the command name for its error messages, and both `dismiss` and `dismiss-inline` now call it. This was flagged as the right move during design review specifically because a third near-identical copy (anticipated for story 13-4) would have been the threshold past which duplication becomes a maintenance liability.
- **The reaction-marker default-to-confused fix from story 13-2's post-review commit (`03cb092`) is carried forward, not re-litigated.** The new "Invoke Python dismiss-inline handler" step uses the same `reaction="confused"` default, flipped to `"done"` only on an explicit marker match — never the bot-suggested `grep -q ... && reaction="done"` form, which propagates `grep`'s non-zero exit under `set -e`.
- Test factory/fixture conventions: `_make_provider(handler)` and `_inline_thread(...)`/`_threads_response(...)` helpers are copied from `tests/python/vcs/test_dismiss_github.py` (story 13-1's established GraphQL-mocking shape for inline-thread scenarios), not `test_cli_dismiss.py`'s BODY-oriented `_finding()`/`format_body_finding` helpers — the inline path never touches review bodies or the feedback store.

### Project Structure Notes

- Modified: `ai_pr_review/cli.py` (new `dismiss-inline` subcommand; new shared `_build_github_provider_or_exit()` helper; `dismiss` refactored to use it).
- Modified: `.github/workflows/slash-commands.yml` (the `dismiss-finding` job only — `dismiss-body-finding` (13-2, done) and `feedback-command` (13-4, backlog) are untouched).
- New test file: `tests/python/test_cli_dismiss_inline.py`.
- No changes to `ai_pr_review/slash/dismiss.py` or `ai_pr_review/vcs/github.py` — `dismiss_inline_reply` and its primitives are already complete from story 13-1.

### References

- [Source: memory-bank/bmad/implementation-artifacts/13-2-wire-dismiss-body-finding.md] — prior story; established the container-job conversion pattern, the base64-reply-round-trip CLI-invocation shape, and the reaction-marker default-to-confused fix this story reuses verbatim
- [Source: ai_pr_review/slash/dismiss.py:376-451] — `dismiss_inline_reply`, unchanged by this story
- [Source: ai_pr_review/slash/dismiss.py:180-194] — `_thread_by_comment_id`, confirms the function was purpose-built to mirror `dismiss-finding`'s bash correlation logic (`comments.nodes[].databaseId == parent_comment_id`, checked across all comments in a thread, not just the first)
- [Source: .github/workflows/slash-commands.yml, `dismiss-finding` job pre-change] — bash implementation being replaced: "React — seen", "Validate parent comment is from the bot" (`id: validate`, preserved), "Find and resolve the review thread" (`id: resolve`, replaced), "Dismiss review if all threads resolved" (replaced), "React — done/not applicable/failed" (updated to key off the new step)
- [Source: tests/python/vcs/test_dismiss_github.py] — `_make_provider`, `_inline_thread`, `_threads_response` test harness conventions reused for the new CLI test file

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- **`--review-id` fallback design decision, reconciled via a review-then-verify cycle:** initial design intent (based on a literal reading of the approved epic plan) was to treat a missing `review_id` as strictly "skip dismissal, never derive a substitute" — mirroring what looked like the bash job's behavior. Writing a test for this (`review_id=None` but the mock thread still carrying a `review_db_id`) failed: `dismiss_inline_reply` (already-merged story 13-1 code) does fall back to `_thread_review_id(target_thread)` when `review_id is None`, and does dismiss in that case. Rather than editing merged story-13-1 code to force strict-skip semantics, verified whether that fallback is reachable in production: spot-checked real inline comments on `tag1consulting/ai-pr-review-test#1`/`#2` via `gh api repos/tag1consulting/ai-pr-review-test/pulls/1/comments --jq '.[] | {review_id: .pull_request_review_id}'` — every comment (30+ checked, including the bot's own review comments) carries a non-null `pull_request_review_id`. Conclusion: the parent comment's `review_id` and the thread's own `review_id` describe the same underlying fact (which review a comment belongs to), so a state where one is null and the other isn't cannot occur in production. The CLI passes `--review-id` straight through without adding any skip/derive logic of its own; the merged fallback in `dismiss_inline_reply` stays as-is. Test corrected to assert the real, reachable behavior instead of an impossible input state.
- Provider-construction duplication: rather than inlining a third copy of the `VCS_PROVIDER` gate + `provider_from_env()` dispatch (the second copy would have been the `dismiss-inline` command itself, following `dismiss`'s existing inline pattern), factored it into `_build_github_provider_or_exit(command_label)` and updated `dismiss` to call it too — a net reduction in `cli.py`'s line count despite adding a new command.
- Local dev-loop friction (not a code defect): running tests from a freshly created worktree initially resolved `ai_pr_review.cli` to the *main checkout's* copy of the file instead of the worktree's, despite a correct editable-install `.pth`/finder mapping. Root cause: the shell's working directory silently reset to the main checkout between tool calls in this environment, and Python's implicit `sys.path[0] = ''` (current-directory) entry took priority over the appended editable-install meta-path finder. Fixed by always `cd`-ing into the worktree within the same shell invocation that runs Python/pytest, rather than relying on a persisted `cd`.

### Completion Notes List

- No deviations from the approved epic plan's story-13-3 scope. The one open design question (whether to change `dismiss_inline_reply`'s `review_id` fallback) was resolved in favor of leaving story 13-1's merged code untouched, per the verification above — documented here rather than silently discovered by a future reader diffing test expectations against source behavior.

### File List

- `ai_pr_review/cli.py` — new `dismiss-inline` subcommand; new `_build_github_provider_or_exit()` helper (also used by `dismiss`, refactored to call it)
- `tests/python/test_cli_dismiss_inline.py` — new test file (8 tests)
- `.github/workflows/slash-commands.yml` — `dismiss-finding` job converted to `container:`; "Find and resolve the review thread" + "Dismiss review if all threads resolved" collapsed into "Invoke Python dismiss-inline handler" + "Post dismiss reply" + "React — dismiss outcome"; "React — not applicable"/"React — failed" retained, now keyed off the new step's outcome where applicable
