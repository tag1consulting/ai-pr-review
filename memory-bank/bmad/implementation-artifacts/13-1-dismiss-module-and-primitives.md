# Story 13.1: Dismiss orchestration module + promoted GitHub primitives

Status: review

PR: https://github.com/tag1consulting/ai-pr-review/pull/556

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a **maintainer of ai-pr-review**,
I want the `/ai-pr-review dismiss` command's F-ID classification, GraphQL thread-resolution, and review-dismissal logic available as tested Python functions (not just inline bash in `slash-commands.yml`),
so that the four bugs already found in this untested code path (#550's fix, PR #553, and its live-e2e follow-up #555) cannot recur silently, and future changes to this logic are pytest-verifiable instead of live-PR-verifiable only.

This is phase 1 of a 4-phase epic (Epic 13, GitHub issue #554). This story ships **zero workflow changes** — it only adds the new module, promotes existing primitives, and adds test coverage. No `slash-commands.yml` job is rewired yet (that starts in story 13-2).

## Acceptance Criteria

1. `ai_pr_review/vcs/github.py`'s `_fetch_review_threads()` is renamed to `fetch_review_threads()` (public) with identical behavior; all internal callers (`resolve_stale`) updated to the new name.
2. `_resolve_thread()` is renamed to `resolve_thread()` (public) with identical behavior; internal callers updated.
3. A new public method `dismiss_review(review_id: int, message: str) -> tuple[bool, int, str]` is added to `GitHubProvider`, implemented as a thin PUT to the existing `_dismiss_url(review_id)` helper. It is **not** a wrapper around `_dismiss_stale_reviews` (that method returns `0`/no-ops whenever `current_review_id is None` — verified at github.py, its docstring confirms the policy is "protect the review the *current run* just posted," which is the inverse of what slash-dismiss needs, since a slash command never posts a new review).
4. A new public method `list_bot_reviews() -> list[dict[str, Any]]` is added, factoring out the paginated `/repos/{owner}/{repo}/pulls/{pr}/reviews` walk that is currently duplicated inside `_list_prior_bot_review_bodies` and `_dismiss_stale_reviews`.
5. ~~`_list_prior_bot_review_bodies()` gains a `require_body_findings_section: bool = True` parameter~~ **SUPERSEDED — see Completion Notes.** Verified against the actual code in the worktree: `_list_prior_bot_review_bodies()` already carries the #553 fix and filters on `ID_MAP_MARKER_PREFIX in body or "**[F" in body`, not the `### Findings not attached to specific lines` string this AC assumed (that string only appears in the method's docstring, describing the pre-#553 behavior it replaced). This filter already includes every body that could carry a classifiable finding — inline-only bodies always carry the id-map marker or an F-token, so the #550 bug class this AC was guarding against cannot recur through this path. No parameter is needed. Instead, `dismiss_by_finding_id` sources its bodies from AC 4's `list_bot_reviews()` directly (`[r.get("body", "") for r in provider.list_bot_reviews()]`), which needs no filtering at all since `classify_finding`/bullet-scan on an empty/irrelevant body is a no-op.
6. A new module `ai_pr_review/slash/dismiss.py` provides:
   - `FindingLocation` enum: `BODY`, `INLINE`, `UNKNOWN`.
   - `ClassifiedFinding` frozen dataclass: `location`, `source`, `file`, `line`, `rule_id` (all defaulting to `""` except `location`).
   - `classify_finding(bodies: Sequence[str], finding_id: int) -> ClassifiedFinding`.
   - `list_active_body_ids(bodies: Sequence[str]) -> list[int]`.
   - `DismissResult` frozen dataclass: `reply`, `thread_resolved`, `review_dismissed`, `feedback_source`, `feedback_file`, `feedback_rule_id`, `active_body_ids`, `errors` (tuple of strings).
   - `dismiss_by_finding_id(provider: GitHubProvider, finding_id: int, *, actor: str, command: str) -> DismissResult`.
   - `dismiss_inline_reply(provider: GitHubProvider, parent_comment_id: int, review_id: int | None, *, actor: str, command: str) -> DismissResult`.
7. **`classify_finding` correctness (this is the load-bearing acceptance criterion — verified against the actual code, not assumed):** it must run its own unconditional bullet-scan across BOTH review-body buckets (the in-diff `### Findings not attached to specific lines` section and the out-of-diff `<details>...Out-of-diff analyzer findings...</details>` block) to check for the literal `**[F<n>]**` token, independent of whether any body carries the id-map marker. It must **not** derive BODY-vs-INLINE from `_parse_existing_ids`'s output — that function's marker fast-path (`_finding_ids.py:96-101`) populates its fingerprint→id map from the marker and `continue`s without scanning bullets when the marker is present, and the marker holds fingerprints for *all* buckets (inline + both body buckets) indiscriminately. Building the classifier on top of `_parse_existing_ids`'s output alone would classify every finding on any marker-bearing review identically. If the bullet-scan finds the token in either bucket → `BODY` (extract source/file/line/rule_id from that bullet, reusing `_finding_ids.py`'s `_SOURCE_RE`/`_LOCATION_RE` regexes, not a hand-rolled sed-equivalent). Else, if `finding_id` is a value in `extract_id_map()`'s combined output across all bodies → `INLINE`. Else → `UNKNOWN`.
8. Dismiss semantics (deliberately chosen — the 4 existing bash copies in `slash-commands.yml` disagree on this, and this story's module standardizes on one policy rather than porting the disagreement): for `dismiss_inline_reply`, the review targeted for dismissal is the one owning the resolved thread (`pullRequestReview.databaseId` of that thread). For `dismiss_by_finding_id`'s BODY case, no dismissal occurs directly (there is no single thread); for its INLINE case, the resolved thread's owning review is targeted, same as `dismiss_inline_reply`. In all cases, "unresolved thread count" is scoped **per-review** (`databaseId == target_review_id`), never PR-wide.
9. `tests/python/slash/test_dismiss.py` (new file, new `tests/python/slash/` directory alongside the existing `tests/python/test_slash_parser.py`/`test_slash_handlers.py`) covers, without HTTP mocking (pure functions): a finding rendered as a body bullet in the in-diff section → `BODY` with correct extracted fields; a finding rendered only in the out-of-diff `<details>` block → `BODY` (this is the #550 regression case — must pass); a finding present in the id-map but not as any bullet → `INLINE`; an unknown finding ID → `UNKNOWN`; a bullet whose source tag is preceded by a severity bracket (`**[High]**`) → source extracted correctly, not the severity string (the #553 source-tag bug, guarded against regression).
10. `tests/python/vcs/test_dismiss_github.py` (new file) follows the `_make_provider(handler)` pattern from `tests/python/vcs/test_github_stale.py` (`httpx.MockTransport` → `httpx.Client` → `RecordingClient` with `RetryPolicy(attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None)`) and covers the #555 bug class specifically: a mocked GraphQL response returning HTTP 200 with a top-level `{"errors": [...]}` body, and a mocked response returning HTTP 200 with a non-JSON/malformed body — both must surface as entries in `DismissResult.errors` with no dismiss PUT issued, proving the Python path cannot silently treat an error response as valid data the way the bash `gh api --jq` call did.
11. `pytest tests/python -q`, `mypy ai_pr_review/`, and `ruff check ai_pr_review/ tests/python/` all pass clean. No pre-existing test in `tests/python/vcs/test_finding_ids.py` or `tests/python/vcs/test_github_stale.py` regresses (run these explicitly before and after to confirm; the OOD regression test `test_marker_less_out_of_diff_ids_preserved_across_reviews` from #553 must still pass — this story does not touch `_finding_ids.py`'s logic, only reuses its regex constants).

## Tasks / Subtasks

- [x] Task 1 — Promote `github.py` primitives (AC: 1, 2, 3, 4, 5)
  - [x] Rename `_fetch_review_threads` → `fetch_review_threads`; update the one internal caller in `resolve_stale`
  - [x] Rename `_resolve_thread` → `resolve_thread`; update the one internal caller in `resolve_stale`
  - [x] Add `dismiss_review(review_id, message)` as a thin wrapper around the existing PUT-to-`_dismiss_url` logic already inlined in `_dismiss_stale_reviews` — extract, don't duplicate
  - [x] Add `list_bot_reviews()`, factoring the paginated reviews-list walk out of `_dismiss_stale_reviews`; left `_list_prior_bot_review_bodies` untouched — its `[]`-on-HTTP-error-mid-pagination return is a deliberate #550/#553 guarantee that `list_bot_reviews()`'s partial-results-plus-`_errors`-append behavior does not preserve, and its existing filter already covers every body the dismiss classifier needs (see AC 5)
  - [x] ~~Add `require_body_findings_section` param~~ superseded — no-op, see AC 5
  - [x] Ran `mypy ai_pr_review/` after this task alone — clean, no missed call sites
  - [x] Extended `fetch_review_threads`'s shared GraphQL query to also select `databaseId` on each comment (raised `comments(first:1)` → `comments(first:100)`) so the same query shape serves both `resolve_stale`'s existing needs and the new `dismiss_inline_reply`'s comment-id correlation, per the plan's "one query shape serves both needs" goal — additive only, `_first_comment_*` helpers still read `nodes[0]` unchanged. Verified via `test_github_stale.py` (all 34 pre-existing tests in that file + `test_finding_ids.py` still pass — the mock harness returns canned data, so this shape change was invisible to response-parsing).
- [x] Task 2 — `ai_pr_review/slash/dismiss.py` classification core (AC: 6, 7)
  - [x] `FindingLocation`, `ClassifiedFinding`, `DismissResult` dataclasses
  - [x] Self-contained bullet-scan (`_scan_body_bullets`) in `dismiss.py`, reusing `_finding_ids.py`'s `_ID_RE`/`_SOURCE_RE`/`_LOCATION_RE` directly; always runs unconditionally, no marker fast-path
  - [x] `classify_finding()` implementing the BODY → INLINE → UNKNOWN precedence from AC 7
  - [x] `list_active_body_ids()`
- [x] Task 3 — Orchestration functions (AC: 8)
  - [x] `dismiss_by_finding_id()`
  - [x] `dismiss_inline_reply()`
  - [x] Reply strings approximate current bash tone; exact wording deferred to story 13-2 when live-e2e wiring can confirm phrasing end to end
  - [x] Both functions drain `provider._errors` (snapshot-before/diff-after, mirroring `resolve_stale`'s established pattern) into `DismissResult.errors` — this is the fix for the #555 failure class: an HTTP error or GraphQL-200-with-errors response from any sub-call (`list_bot_reviews`, `fetch_review_threads`, `resolve_thread`) is now impossible to silently lose, unlike the bash `gh api --jq` call that started this epic
  - [x] Same-call dismiss uses in-memory thread-state update (`target_thread["isResolved"] = True` on the live reference right after a successful resolve), not a second GraphQL fetch — a re-fetch would introduce a fresh #555 surface (a GraphQL-200-with-errors response there would read as "zero unresolved threads" and cause an erroneous dismiss); the in-memory approach makes that failure mode structurally impossible instead of merely observable. Deliberate deviation from the bash's literal mechanism (which re-queries), while matching its same-call-dismiss *behavior* — the epic's mandate is "pick one canonical policy," not "replicate the mechanism."
- [x] Task 4 — Tests (AC: 9, 10, 11)
  - [x] `tests/python/slash/test_dismiss.py` — 9 tests, pure classification, no HTTP
  - [x] `tests/python/vcs/test_dismiss_github.py` — 8 tests, HTTP-mocked via `_make_provider`, including 3 covering the #555 class (GraphQL-200-with-errors and HTTP-error-status, for both orchestration functions) and the never-dismiss-on-fetch-error invariant
  - [x] Full-suite regression pass: `pytest tests/python -q` (1705 passed), `mypy ai_pr_review/` (88 source files, clean), `ruff check ai_pr_review/ tests/python/` (clean)

## Dev Notes

- This story has an approved implementation plan with additional design rationale, verified file:line references, and a table comparing how the 4 existing bash copies of the dismiss/resolve logic disagree: `/home/gchaix/.claude/plans/please-plan-how-to-linked-leaf.md`. Read it before starting — it documents *why* each design choice in the acceptance criteria was made, including a rejected alternative (routing through `resolve_stale`/`_dismiss_stale_reviews`) and why it doesn't work for this use case.
- GraphQL support already exists in this codebase (`_fetch_review_threads`, `_resolve_thread` in `github.py`) — this is not new capability, only promotion to public + one new sibling method (`dismiss_review`).
- `ai_pr_review/vcs/marker.py` has `INLINE_MARKER`, `ID_MAP_MARKER_PREFIX`, `extract_id_map()` — reuse these directly, do not reimplement marker parsing.
- `ai_pr_review/vcs/_finding_ids.py` has the authoritative `_SOURCE_RE`/`_LOCATION_RE` regexes for reverse-parsing a rendered bullet back into source/file/line — reuse these in the classifier's bullet-extraction rather than writing new regexes, to avoid reintroducing the severity-vs-source bug that was already fixed once (#553).
- `ai_pr_review/vcs/_body.py`'s `format_body_finding()` is the authoritative *forward* renderer of the bullet format the classifier parses backward — if a future change touches bullet rendering, the classifier's bullet-scan must be updated in lockstep. Not this story's concern, but worth a comment in the new module pointing at `_body.py` for whoever touches this next.
- Test factory convention varies slightly across existing test files: some use `Finding.model_validate({...} | kwargs)` (`tests/python/test_findings.py`), others construct `Finding(...)` directly with kwargs (`tests/python/vcs/test_finding_ids.py`). `Finding` is a pydantic `BaseModel`, so both work; match whichever convention the file you're extending already uses. `test_dismiss.py` is new, so either is fine — prefer the direct-kwargs style since it's what `test_finding_ids.py` (the closest sibling module) uses.

### Project Structure Notes

- New file: `ai_pr_review/slash/dismiss.py` (alongside existing `ai_pr_review/slash/parser.py`, `ai_pr_review/slash/handlers.py`).
- New directory + file: `tests/python/slash/test_dismiss.py` (no `tests/python/slash/` directory exists yet; the existing `test_slash_parser.py`/`test_slash_handlers.py` live directly under `tests/python/`, not in a subdirectory — check whether to match that flat convention instead of introducing a new subdirectory, and use whichever this repo's existing `tests/python/` layout actually supports without a new `__init__.py`/conftest wiring need).
- Modified: `ai_pr_review/vcs/github.py` (renames + 2 new public methods + 1 new parameter).
- New file: `tests/python/vcs/test_dismiss_github.py`.
- No changes to `.github/workflows/slash-commands.yml`, `ai_pr_review/cli.py`, or `container-action/action.yml` in this story — those are stories 13-2 through 13-4.

### References

- [Source: /home/gchaix/.claude/plans/please-plan-how-to-linked-leaf.md] — full approved plan, design rationale, verified bash-copy disagreement table
- [Source: ai_pr_review/vcs/github.py] — `_fetch_review_threads`, `_resolve_thread`, `_dismiss_stale_reviews`, `_list_prior_bot_review_bodies`, `resolve_stale`, `_dismiss_url`
- [Source: ai_pr_review/vcs/_finding_ids.py] — `_parse_existing_ids` (marker fast-path at lines 96-101, the source of AC 7's constraint), `_SOURCE_RE`, `_LOCATION_RE`, `fingerprint`
- [Source: ai_pr_review/vcs/marker.py] — `INLINE_MARKER`, `ID_MAP_MARKER_PREFIX`, `extract_id_map`
- [Source: tests/python/vcs/test_github_stale.py] — `_make_provider(handler)` mock-transport test harness to follow
- [Source: memory-bank/bmad/implementation-artifacts/10-1-vcs-partition-dedup.md] — most recent related story in `ai_pr_review/vcs/`; established the `list(findings)` mypy-gotcha pattern and confirmed scope-boundary discipline (explicitly listing what's out of scope) that this story follows

## Dev Agent Record

### Agent Model Used

claude-sonnet-5

### Debug Log References

- AC 5 required correction mid-implementation: `_list_prior_bot_review_bodies()` was found (by direct code read, not assumption) to already carry the #553 fix, filtering on `ID_MAP_MARKER_PREFIX in body or "**[F" in body` rather than the `### Findings not attached to specific lines` string the AC had assumed (that string only ever appeared in the method's docstring, describing the *pre-#553* behavior it replaced). The `require_body_findings_section` parameter this AC specified would have been a no-op. Resolution: `dismiss_by_finding_id` sources its bodies from `list_bot_reviews()` directly instead, which needs no filtering at all. See AC 5's strikethrough note and Task 1's corresponding subtask for the full trace.
- `fetch_review_threads`'s GraphQL query needed extending beyond what the story anticipated: `dismiss_inline_reply`'s comment-id correlation (matching a REST `parent_comment_id` against a GraphQL thread) requires `databaseId` on each comment, which the pre-existing query never selected (it only fetched `comments(first:1){nodes{body author{login} pullRequestReview{databaseId}}}` for `resolve_stale`'s narrower needs). Extended to `comments(first:100){nodes{databaseId body author{login} pullRequestReview{databaseId}}}` — additive, verified non-regressing via the full `test_github_stale.py` suite.
- Found and fixed a same-call-dismiss bug during test-writing: the first implementation reused the pre-resolve `threads` snapshot for the "are all threads resolved" check after resolving the target thread, which meant dismissal could never happen in the same command invocation (mirroring `resolve_stale`'s deferred-to-next-run semantics, which is correct for that frequently-running path but wrong for a user-invoked slash command, which expects the effect now). Fixed via in-memory snapshot update rather than a second GraphQL fetch, which would have reintroduced a #555-shaped failure surface at the exact point where a false "zero unresolved" reading would cause an erroneous dismiss.
- **Flagged, unverified, deliberately not touched:** `dismiss.py`'s three `is_owned_by_us(...)` calls pass `bot_login=None` (author-login check skipped, marker is the sole gate). This differs from `resolve_stale`/`_dismiss_stale_reviews`, which pass `self.config.bot_login` (the REST-style constant `"github-actions[bot]"`) against the same GraphQL-sourced author field. Memory `reference_bot_login_graphql_vs_rest` claims GitHub's GraphQL API reports the bot's login without the `[bot]` suffix (`"github-actions"`), which would make that REST-style comparison a silent no-op — this memory's claim was **not independently verified in this session** (live verification is blocked anyway: the `GH_TOKEN` PAT on `ai-pr-review-test` is the very thing found broken during #555's investigation). `None` is the safe choice under either hypothesis (see the in-code comment on `_dismiss_if_all_resolved` for the full reasoning), so this story keeps `None` and does not modify `resolve_stale`/`_dismiss_stale_reviews` (out of scope — Task 1 is renames-with-identical-behavior only). A locking test (`test_dismiss_inline_reply_graphql_style_author_still_owned`) pins the `None` choice so a future "fix" to `bot_login` fails loudly instead of silently reintroducing the #378 bug class. **Action for 13-2:** confirm the actual GraphQL author-login format via live-e2e smoke testing (once the PAT is fixed) and decide whether `resolve_stale`/`_dismiss_stale_reviews`'s `bot_login` comparison needs the same `None` treatment — that would be a separate, out-of-epic bugfix, not part of #554.

### Completion Notes List

- All 11 acceptance criteria satisfied except AC 5, which was superseded by a correction (see Debug Log References and AC 5's strikethrough text) — the underlying need (dismiss classification must not lose inline-only bodies) is still met, via a different mechanism (`list_bot_reviews()` sourcing) than originally specified.
- Zero workflow changes made, per this story's explicit scope boundary — `.github/workflows/slash-commands.yml`, `ai_pr_review/cli.py`, and `container-action/action.yml` are all untouched.
- `provider._errors` draining (snapshot-before, diff-after) is the load-bearing mechanism for AC 10 and the whole story's motivating premise (issue #555): both `dismiss_by_finding_id` and `dismiss_inline_reply` now surface every HTTP/GraphQL error from every sub-call into `DismissResult.errors`, with dedicated regression tests proving no dismiss/resolve action is ever taken on top of an error response.
- Full verification trio green: `pytest tests/python -q` (1706 passed, up from the pre-story baseline plus 18 new tests across the two new test files), `mypy ai_pr_review/` (88 source files, clean), `ruff check ai_pr_review/ tests/python/` (clean, after one auto-fix for import ordering in the new test files and one manual fix for an unused variable left over from the `list_bot_reviews()` extraction in `_dismiss_stale_reviews`).
- Verified `_build_inline_comment_body` (github.py:927) does emit `**[F{finding_id}]**` for inline findings, confirming the INLINE classification branch in `dismiss_by_finding_id`/`dismiss_inline_reply` targets a real, reachable rendering (not an assumed one) — this closes a gap flagged during self-review where the INLINE path's `[F{n}]` substring match had only been proven against a hand-built test fixture, not the actual producer.
- See the flagged, unverified `bot_login=None` item above — the one open question this story defers to 13-2 rather than resolving speculatively.

### File List

- `ai_pr_review/vcs/github.py` — modified: `_fetch_review_threads` → `fetch_review_threads` (renamed, query extended with comment `databaseId`), `_resolve_thread` → `resolve_thread` (renamed), new `dismiss_review()`, new `list_bot_reviews()`, `_dismiss_stale_reviews()` refactored to use `list_bot_reviews()`/`dismiss_review()`, updated caller sites in `resolve_stale()`, docstring fix on `list_bot_reviews()`, removed one dead `c = self.config` local
- `ai_pr_review/slash/dismiss.py` — new file: `FindingLocation`, `ClassifiedFinding`, `DismissResult`, `_scan_body_bullets()`, `classify_finding()`, `list_active_body_ids()`, `_first_comment*`/`_thread_review_id`/`_thread_by_comment_id` helpers, `_dismiss_if_all_resolved()`, `dismiss_by_finding_id()`, `dismiss_inline_reply()`
- `tests/python/slash/__init__.py` — new file (empty, package marker)
- `tests/python/slash/test_dismiss.py` — new file: 9 tests covering classification precedence, both body buckets, inline-via-id-map, unknown, severity-vs-source regression, and `list_active_body_ids`
- `tests/python/vcs/test_dismiss_github.py` — new file: 9 tests covering BODY/INLINE/UNKNOWN orchestration outcomes, same-call resolve+dismiss, the #555 error-surfacing class for both orchestration functions, and a locking test for the `bot_login=None` design choice

**Note for 13-2:** `dismiss_by_finding_id`'s BODY branch sets `feedback_rule_id=classified.source` (via `_scan_body_bullets`), but the bash body-finding path set `SLASH_RULE_ID: ''` unconditionally (slash-commands.yml, "Record false-positive in feedback store" step). If the feedback store keys suppression on `rule_id`, wiring this in 13-2 will write a different key than bash did for the same finding — check `ai_pr_review/feedback/store.py`'s suppression-matching logic before wiring the CLI subcommand.
- `memory-bank/bmad/implementation-artifacts/sprint-status.yaml` — Epic 13 added, story 13-1 status progression tracked
- `memory-bank/bmad/implementation-artifacts/13-1-dismiss-module-and-primitives.md` — this file; AC 5 correction, task checkboxes, Dev Agent Record
