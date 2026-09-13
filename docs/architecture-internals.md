# Architecture (Internal Reference)

This document contains deep implementation details for maintainers and AI agents working on ai-pr-review. For the high-level directory layout and data flow, see [docs/architecture.md](architecture.md). For contributor how-tos, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Agent output schema

Every agent prompt expects a `json-findings` fenced code block in the response:

```json
[
  {
    "severity": "Critical|High|Medium|Low",
    "confidence": 0-100,
    "file": "path/to/file.ext",
    "line": 42,
    "start_line": 40,
    "finding": "Description of the issue",
    "remediation": "How to fix it",
    "suggested_code": "replacement code"
  }
]
```

The `source` field is optional in agent output — the findings extractor stamps the agent name automatically if the field is absent. Static analyzers hard-code their source values in their output projection.

`suggested_code` and `start_line` are optional fields emitted when the `enable-suggestions` action input is `true` (the default). See [Code suggestions](#code-suggestions) below.

Findings below confidence 75 are filtered out. Duplicates are deduped using proximity-based matching: findings in the same file within 3 lines of each other are merged into a single cluster, keeping the highest-severity finding. The dedup step carries a `sources` array on the surviving finding, unioning all sources from the cluster. When multiple sources are present, the VCS provider renders `[first-source] *(also flagged by: other)*` attribution (sources are sorted alphabetically).

## Runtime flow

The entrypoint is `ai_pr_review.cli:review`. The Click command parses flags, sets up logging, and calls `_run_review_async()`, which:

1. Calls `build_review_runtime(config)` in `ai_pr_review/review/runtime.py`. The runtime layer:
   - Resolves provider model defaults (`ReviewConfig.resolve_models()`).
   - Builds the VCS provider via `provider_from_env`.
   - Fetches the last-reviewed SHA and computes the diff (`ai_pr_review/review/compute.py`).
   - Returns `SkipPlan` if compute reports no changes.
   - Loads the feedback store (`AI_FEEDBACK_LOOP=1`) into a feedback addendum.
   - Fetches the PR/MR title and description via `provider.get_pr_description()` (fail-soft: a missing or malformed result just omits this) and folds it with the file manifest into the shared `<pr-context>` block (`ai_pr_review/review/pr_context.py:build_shared_context_block`, #813).
   - Resolves `.github/ai-pr-review/policy.yml` if present (`ai_pr_review/policy.py`) and merges any matched route's agent/analyzer allow-deny lists and review-mode default with the explicit config (explicit input always wins). See [docs/policy.md](policy.md) for the policy-file format; this also determines `policy_gate_required`/`policy_gate_satisfied` for the CLI's merge-gate check-run.
   - Detects changed file languages and loads the whole text of every detected language's profile once via `load_language_profiles()`. The concatenated markdown is stored in `DispatchContext.language_profile_text` so each agent dispatch reads from memory rather than disk (#814: every eligible agent gets the whole profile, not a per-agent routed subset — see [Shared run-context assembly](#shared-run-context-assembly) below).
   - Runs native analyzers, loads SARIF findings (via `config.sarif_paths`), loads suppression rules, evaluates gates, and builds the `DispatchContext` and `OrchestrationConfig`. All pre-computed findings are merged into `OrchestrationConfig.extra_findings`.
2. Runs `pr-summarizer` on first (non-incremental) reviews, then `issue-linker` on first reviews in `full` mode when the VCS provider is GitHub (both fail-soft; see `ai_pr_review/review/preflight.py`).
3. If `AI_DRY_RUN=1`, short-circuits after assembly without posting.
4. Otherwise calls `orchestrate.run_review()`, which dispatches agent tiers, merges LLM + pre-computed findings via `extra_findings`, suppresses, classifies the outcome, and posts via the provider.

**Boundary:** `run_review()` reads no environment and constructs no dependencies — it is the unit-testable core. `build_review_runtime()` is the seam between env-driven configuration and pure orchestration. Tests for the assembly layer are in `tests/python/test_runtime.py`.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment embedded in the PR/MR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). The VCS provider extracts it at the start of each run. Subsequent pushes diff from that SHA to HEAD. The watermark is advanced at the end of each run when the summary comment is posted with the new HEAD SHA.

The VCS provider keeps at most one summary comment on the PR/MR by cleaning up duplicates after each upsert. Duplicates can accumulate when two runs fire concurrently. Cleanup is non-fatal: DELETE failures emit a WARNING.

To force a full-PR diff for a single run, add the `ai-review-rescan` label to the PR. The workflow sets `FORCE_FULL_DIFF=true` via the `env:` block, which causes the engine to skip the last-reviewed SHA lookup and fall through to the full `origin/BASE_REF...HEAD_SHA` diff.

## Canonical-review reuse (GitHub)

`ai_pr_review/vcs/_canonical.py` is pure classification logic (no I/O) consumed by `GitHubProvider.post_findings` (`ai_pr_review/vcs/github.py`) to decide whether a rerun can `PUT` the existing "canonical" review's body instead of always `POST`ing a new review object.

**Canonical review**: `select_canonical(reviews)` — the highest-`id` bot review with a non-empty body among `GitHubProvider._list_prior_bot_reviews()`'s output, regardless of state. Reviews with an empty or whitespace-only body are skipped: GitHub auto-creates one of these (state `COMMENTED`) every time the bot replies to an inline comment via the REST replies endpoint (the severity-escalation notice, a recurred-finding reply, a feedback acknowledgement), and GitHub rejects any attempt to `PUT` a body onto such a review with HTTP 422. `ai_pr_review.slash.dismiss._record_verdict` (the write side) calls this same function directly rather than reimplementing the rule, so both sides of the split are structurally guaranteed to agree on which review is canonical *given the same review list*. `_record_verdict` itself is fed from the separate `list_bot_reviews()` method, which keeps a looser contract than `_list_prior_bot_reviews()` (partial results on a mid-pagination error rather than `[]`, and no `PENDING`-state filter) — both methods now share one paginated walk (`GitHubProvider._list_reviews_paginated(*, strict, states)`), so they can no longer *independently* drift on pagination or error-handling bugs, but the two contracts remain deliberately different: `list_bot_reviews()`'s other callers (`ai_pr_review.slash.dismiss`'s F-id classification, and the PR-wide auto-approve check) need every review regardless of state and tolerate a partial fetch, so unifying the contract there would cost real functionality for a discrepancy window that, in practice, never opens — the bot always submits its own reviews with an explicit `event`, so it never leaves one in `PENDING`. A review in `PENDING` state or a fetch error affecting only one side can therefore still, in principle, make the two sides disagree about which review is canonical; this is an accepted, documented tradeoff, not a bug this consolidation set out to close.

**Markers** (`ai_pr_review/vcs/marker.py`):
- `ai-pr-review-id-map` — fingerprint → stable `F<n>` ID, unchanged by this feature.
- `ai-pr-review-verdicts` — fingerprint → `"dismissed"` | `"fixed"` | `"recurred"`, written by slash commands (`_record_verdict`) and read by `classify()`. `"recurred"` is a tombstone: written in place of deleting a `"fixed"` entry when that finding reappears, since `merge_verdicts()` unions the marker across *every* prior review body (not just the canonical's own) and a bare delete on the newest body wouldn't remove the key from an older body still in that union.
- `ai-pr-review-finding` (new) — per-inline-comment, base64-encoded `{fp, cat, sev}`. Category and severity are otherwise unrecoverable from a rendered comment (category is never rendered anywhere; severity is only re-parsable from the `**[Sev]**` header token). Base64-encoded rather than raw JSON because `suggested_code` is interpolated unescaped ahead of it in the same comment body — a raw-JSON marker could be forged or duplicated inside a rendered code fence.

**Per-finding decision** (`classify()`), in order: exact or category/severity-gated fuzzy match against a `"dismissed"` verdict → suppressed forever; exact match against a `"fixed"` verdict → recurred (reply + `unresolveReviewThread`, verdict becomes `"recurred"`); fuzzy `(file, ±3 lines, compatible category)` match against a still-open owned thread → escalate (PATCH + reply) if severity increased, else update (PATCH) — a fuzzy match that lands on an *outdated* thread is reclassified as `new` instead of update/escalate, since GitHub already renders that thread as invisible in its UI; otherwise new. When two distinct findings in the same run both fuzzy-match the same open thread, `dedupe_thread_claims()` keeps only the higher-severity claim and demotes the other back to `new`, so a single `PATCH`/reply can't silently absorb two findings.

Thread ownership (`parse_prior_thread`, `resolve_stale`, `_dismiss_stale_reviews`) gates on the per-comment marker plus the author login, normalized via `ai_pr_review.vcs._stale.graphql_bot_login()` before comparison — GitHub's GraphQL API reports the bot's login without the REST API's `"[bot]"` suffix (verified live against a real thread), so comparing the raw `self.config.bot_login` constant against it rejected every real thread (issue #717). Normalizing rather than passing `bot_login=None` (the more conservative fix used by `ai_pr_review.slash.dismiss` for GraphQL-sourced authors, chosen there before the exact format difference was confirmed) keeps the author check as a real defense-in-depth signal against a spoofed marker. `post_findings` additionally tracks, per run, which threads it PATCHed (`update`/`escalate`) or reopened (`recurred`) in `GitHubProvider._kept_alive_thread_ids`, and `resolve_stale` skips those — without it, the very next `resolve_stale` call (`orchestrate.py` runs them back-to-back) would immediately resolve a thread this feature just deliberately kept open (issue #718).

**Fingerprint drift on the `update` path (issue #720).** The `update`/`escalate` fuzzy match is deliberately loose (same file, within `PROXIMITY_LINES`, compatible category), so the matched finding's exact `fingerprint()` often differs from what the thread was originally posted with (reworded text, or a small line shift). `_apply_thread_update` (`github.py`) PATCHes the comment in place with the *new* fingerprint in its metadata marker, but the visible `**[F<n>]**` token never changes (it's `thread.finding_id`, carried forward unmodified). Two things used to go stale as a result:

- A verdict a human recorded via `/ai-pr-review dismiss` while the comment still showed its *old* fingerprint would hit `_fuzzy_dismissed_match` in a later run, but `_find_thread_by_fingerprint(all_threads, old_fp)` would come back empty, because the thread's own marker had moved on to the new fingerprint. With no thread to recover severity/category from, the match aborted and the dismissed finding reposted as `new`.
- `id_map` (the fingerprint to `F<n>` map) is assembled once, before classification runs, so a reworded finding under `update`/`escalate` classification got a *second*, freshly minted `F<n>` id that was baked into the review body's id-map marker but never rendered anywhere: a wasted id, and a trap for `dismiss.py`'s reverse F-id-to-fingerprint lookup, which would resolve the visible token to the wrong (or no) fingerprint.

Both are closed without touching the fuzzy-match tolerance itself:
- `marker.build_inline_meta_marker`/`InlineMeta` gained an optional `prior_fingerprints` (`"pfp"` in the encoded payload), holding every fingerprint a thread's comment has ever carried, capped at 20 entries. `_apply_thread_update` populates it with the thread's current fingerprint (plus whatever it already carried) whenever a PATCH is about to replace it. `PriorThread.prior_fingerprints` surfaces this, and `_find_thread_by_fingerprint` checks it alongside the thread's current fingerprint, so a verdict recorded against any fingerprint the thread has ever shown still resolves back to it.
- `GitHubProvider.post_findings` re-points `id_map[fingerprint(finding)]` at the matched thread's existing `finding_id` for every `update`/`escalate` classification, right after `classify()`/`dedupe_thread_claims()` run and before the id-map marker is rendered. The id-map then never mints a second, invisible id for a thread that already has one, and a *future* dismiss's reverse lookup resolves to the fingerprint the comment is actually showing.

**Posting decision** (`decide_action()`): `PUT` the canonical body when it exists, isn't `DISMISSED`, its state matches this run's event, it carries the review footer (guards against overwriting a human-facing message like `submit_approval`'s), no `new` finding actually landed an inline comment (`any_new_inline_eligible`, computed from which findings ended up in `inline_comments` after `partition_findings`, not raw `is_inline_eligible` — a finding `max_inline` bumped to the body or whose payload build failed must not count as if it got an inline slot), and no `new` finding is High/Critical severity **unless its exact fingerprint is already in `known_fingerprints`** (`ai_pr_review.vcs._finding_ids.known_fingerprints(prior_bodies)` — every fingerprint already visible in some prior review body). That exemption is what lets a persistent out-of-diff High/Critical finding, or any PR over `max_inline`, stop forcing a fresh review after its first appearance (issue #719): the PUT still renders it in the body every time, so nothing is hidden by not forcing a POST for content a human has already seen. Otherwise `POST` a fresh review carrying only the `new` findings. The prior `CHANGES_REQUESTED` review is dismissed **after** confirming the fresh POST actually landed as a non-degraded `REQUEST_CHANGES` review (never before — dismissing first and having the post then degrade to a `COMMENT` review or a plain issue comment would leave the PR silently unblocked), and only when it has zero unresolved owned threads on an unbroken thread fetch — an incomplete/failed `fetch_review_threads()` call is never treated as "zero unresolved threads" (the same gate `_dismiss_stale_reviews` already applies for the thread count itself, so the slash-command PR-wide auto-approve check can't be fooled by a dismissed-but-still-active review).

**Concurrency**: immediately before the canonical review's body `PUT`, `_try_put_canonical` re-fetches its state/body (`get_review_state_and_body`) and the PR's current head SHA (`get_pr_head_sha`). A mismatch on either falls through to posting a fresh review (state/body changed) or skips the write entirely (head advanced — a newer run already owns the canonical). **This guard covers only the body `PUT`** — the per-thread `PATCH`/reply/`unresolveReviewThread` side effects and the dismiss-superseded-review call have no equivalent re-check, so this narrows rather than eliminates the race window even for the write it does cover; consumers pushing rapidly should still set a GitHub Actions `concurrency:` group keyed on PR number (see `examples/workflows/pr-review.yml`).

GitHub-only; GitLab and Bitbucket still post fresh content every run (tracked in issue #710).

**Shared CRUD/thread helpers (#822).** `ai_pr_review/vcs/_upsert.py` (`upsert_comment`/`advance_sha_marker`) and `ai_pr_review/vcs/_thread.py` (`first_comment*` GraphQL accessors, `count_unresolved_owned_threads`) factor out the find-or-create-then-update control flow and the GraphQL review-thread reads that all three providers' summary-comment CRUD and `github.py`'s/`slash/dismiss.py`'s thread-counting logic previously duplicated. Provider-specific bits (marker/body/footer text, HTTP verb, payload shape) stay in each provider module — only the byte-identical control flow moved.

## Shared run-context assembly

Every finding-producing agent except `blind-hunter` (deliberately excluded — it is meant to reason about the diff with zero project context, see `AgentSpec.context_enrichment_eligible=False`) receives up to three run-shared prompt fragments, assembled once per run in `build_review_runtime()` and threaded through `DispatchContext`:

- **`shared_context_block`** (#813) — the PR/MR title and description (via `provider.get_pr_description()`, truncated to 4000 chars with HTML-comment template boilerplate stripped) plus the changed-files manifest, wrapped in a `<pr-context>` block by `ai_pr_review/review/pr_context.py:build_shared_context_block()`. Empty when the title, body, and manifest are all empty.
- **`language_profile_text`** (#814) — the whole concatenated markdown of every language profile detected in the diff's changed files, loaded once via `load_language_profiles()`. Retired: the older per-agent *routed subset* selection (`ProfileRouter`/`language_profile_sections.py`) that used to make this block differ per agent; every eligible agent now gets the same, whole text.
- **`feedback_addendum`** — recent learnings from the feedback-loop store (`AI_FEEDBACK_LOOP=1`), the least stable of the three since it changes as the store accumulates new entries between reruns.

`ai_pr_review/agents/dispatch.py`'s `_run_single_agent` joins whichever of these are non-empty (most-stable-first: context block, then language profiles, then feedback) into `LLMRequest.system_prefix` (for providers without multi-breakpoint caching) and, separately, into `LLMRequest.cache_blocks` — an ordered tuple of up to 3 independently-cache-tagged fragments consumed by the Anthropic/Bedrock client (see [Prompt caching](#prompt-caching) below). There is no "context variant"/cache-cohort concept in the current code: every eligible agent receives the same fragments in the same order, and per-agent differentiation comes only from each agent's own system prompt (base prompt + governance + knowledge-cutoff + findings-trailer + optional suggestion-addendum, see [Code suggestions](#code-suggestions)).

## Parallel agent execution

Phase 1 runs agents in a tiered fan-out mode by default, reducing wall-clock time from ~5-7 minutes to ~2 minutes in `full` mode.

Disable via `parallel: false` action input or `AI_PARALLEL=false` env var (default: `true`).

> **Breaking change (direct-script users):** Prior to issue #73, the engine defaulted to `AI_PARALLEL=false` when the variable was unset, while `action.yml` defaulted to `true`. The default is now `true` in both invocation paths.

### Tier groupings

| Tier | Agents | When |
|------|--------|------|
| Tier 1 | `pr-summarizer` (first run only, dispatched separately — see below), `code-reviewer`, `silent-failure-hunter` (conditional: `has_error_patterns` gate) | Always |
| Tier 1 (static analyzers, concurrent with Tier 1) | All native analyzers (`ai_pr_review/analyzers/`) | Always (graceful no-op if binary absent) |
| Tier 2 | `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general` | `review-mode: full` only |

Tier 1 and Tier 2 are separated by a barrier so Tier 2 never starts until all Tier 1 agents complete.

**Model selection.** `ai_pr_review/agents/dispatch.py`'s `_run_single_agent` uses the premium model only when `spec.tier == 2 AND mode == "full" AND premium_model is set`; every Tier 1 agent (including `silent-failure-hunter`) always runs on the standard model, in both `quick` and `full` mode. A Tier 2 agent's premium call that is blocked by the provider's content filter (`stop_reason=refusal`, exit code 3) retries once on the standard model for that one call (`fallback_from_model`, #810) rather than dropping the agent's coverage entirely.

`pr-summarizer` and `issue-linker` are marked `separately_dispatched=True` in the roster and never go through `run_tier` — they compose their own prompt and user message (manifest, commit log, diff for `pr-summarizer`; the open-issues list for `issue-linker`) and are called directly from `cli.py` before the tiered fan-out (see [Runtime flow](#runtime-flow)).

## Multi-provider support (GitHub / Bitbucket Cloud / GitLab)

Since v0.2.0 the same container image drives PR/MR reviews on GitHub, Bitbucket Cloud, and GitLab. The provider is selected via the `VCS_PROVIDER` env var:

| `VCS_PROVIDER` | Provider | Python module |
|---|---|---|
| `github` (default) | GitHub | `ai_pr_review/vcs/github.py` |
| `bitbucket` | Bitbucket Cloud | `ai_pr_review/vcs/bitbucket.py` |
| `gitlab` | GitLab | `ai_pr_review/vcs/gitlab.py` |

The provider is resolved once at startup via `provider_from_env` and passed through `ReviewRuntime`. Invalid provider values fail fast with a clear error.

## Code suggestions

When `enable-suggestions` is `true` (the default), eligible LLM agents emit an optional `suggested_code` field (and optional `start_line` for multi-line replacements). The VCS provider posting layer wraps these in a provider-native suggestion fence.

**Eligible agents** (system prompt is augmented with `prompts/suggestion-addendum.md`): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`.

Not eligible: `architecture-reviewer`, `adversarial-general`, `pr-summarizer`. Static analyzers never emit suggestions.

**Prompt composition.** The dispatch layer composes the base prompt with up to four shared trailers at runtime:
- `prompts/_governance.md` — Asimov's Three Laws stated explicitly (First and Second binding, Third stated and rejected), then five operational rules: drop self-refuting findings, severity-by-harm, don't-reinvent-the-wheel detection, verify-before-naming plus secret redaction, and obey recorded maintainer verdicts from the `<repo-feedback>` block. Applied to all 7 finding-producing agents (not `pr-summarizer`). Always-on; no env var toggle.
- `prompts/_knowledge-cutoff.md` — HARD CONSTRAINT block against version-existence hallucinations. Applied to all 7 finding-producing agents (not `pr-summarizer`).
- `prompts/_trailer-findings.md` — `json-findings` schema instruction. Applied to all 7 finding-producing agents.
- `prompts/suggestion-addendum.md` — "Apply suggestion" formatting. Gated by `AI_ENABLE_SUGGESTIONS`; applied only to the 5 eligible agents.

Composition order: base prompt + governance + knowledge-cutoff + findings-trailer + (optional) suggestion-addendum. The order is deliberate for Anthropic prompt-cache locality — the existing `_knowledge-cutoff → _trailer-findings → suggestion-addendum` byte sequence at the tail is preserved unchanged.

**Validation guards** (applied in the VCS provider posting layer):
1. `start_line` must be a positive integer and be <= `line`.
2. Multi-line ranges are capped at `MAX_SUGGESTION_RANGE=100` lines.
3. `suggested_code` containing triple backticks is rejected (fence escape prevention).
4. For multi-line suggestions, every line in `start_line..line` must appear in the diff's new-file side.
5. When any guard fails the suggestion is dropped with a WARNING; the finding still posts with natural-language remediation.

**Body finding rendering.** When a finding with `suggested_code` is routed to the review body, the provider posting layer renders the suggestion as a plain code fence inside the collapsible `<details>` accordion. The triple-backtick sanitization guard applies to body suggestions as well.

**Bitbucket** does not render suggestion fences. **GitLab** supports suggestion fences using GitLab's native `suggestion` syntax.

## Suppressions

`config/suppressions.json` is a JSON array of suppression rules evaluated against merged findings. All `match` fields are optional and ANDed:

- `file` — substring match on finding's `file`
- `line` — exact integer match
- `code` — finding text starts with this prefix
- `pattern` — regex (case-insensitive) matched against finding text

An optional `verify` field triggers pre-suppression verification:

| Value | Extracts | Checks |
|-------|----------|--------|
| `github-release` | `owner/repo@vN` | `gh api repos/{owner}/{repo}/git/ref/tags/{tag}` |
| `npm` | `pkg@version` or `"pkg": "version"` | `registry.npmjs.org/{pkg}/{version}` |
| `pypi` | `pkg==version` | `pypi.org/pypi/{pkg}/{version}/json` |
| `go-module` | `module@vX.Y.Z` | `proxy.golang.org/{module}/@v/{version}.info` |
| `cargo` | `pkg = "version"` or `pkg@version` | `crates.io/api/v1/crates/{pkg}/{version}` |
| `docker-hub` | `image:tag` or `ns/image:tag` | `hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags/{tag}` |

If verification confirms the version exists, the suppression stands. If the API returns a non-zero exit, the finding is kept. Private registries (GHCR, GCR, ECR) are not supported.

Consuming repos can add **local suppressions** at `.github/ai-pr-review/suppressions.json`, merged with global rules at runtime.

## LLM judge pass

After the findings pipeline (extract → merge → suppress → diff-scope) produces its final candidate list, `ai_pr_review/findings/judge.py:judge_findings()` — Phase 2.75, gated by `AI_JUDGE_PASS` (default `true`) — sends one compact LLM call on the standard model asking a cheap model to return a `keep` or `downrank` verdict per finding.

- `downrank` is the only non-`keep` verdict; there is no `drop`. The judge never removes a finding outright — a false positive that stays visible is preferred over a silently dropped true positive. `downrank` lowers confidence by `JUDGE_DOWNRANK_AMOUNT` (15) and routes the finding to the review body (`demoted_to_body=True`) instead of an inline comment; severity is left unchanged, since downranking affects placement, not assessed risk.
- Findings with `Finding.corroborated=True` (independently confirmed by both an LLM agent and a static analyzer, see `findings/provenance.py`) are always kept regardless of the judge's verdict — one cheap-model call cannot override an independent, cross-source agreement.
- Always fail-soft: any LLM error, parse error, timeout, or empty input returns the findings unchanged (still `keep`) with a logged WARNING. `JudgeResult` also carries the pass's own token usage (`input_tokens`/`output_tokens`/`cache_creation_tokens`/`cache_read_tokens`), surfaced in the token usage table (see below) alongside the finding-producing agents.

## Token usage and cost estimation

Token counts are accumulated per agent across all LLM calls. For Google Gemini, `cache_read` reports `cachedContentTokenCount` when present; thinking tokens (`thoughtsTokenCount`) are added to the output count since they are billed at the output rate.

`config/model-pricing.json` maps model ID patterns to display names and per-token rates. Each entry carries four rates: `input_rate`, `output_rate`, `cache_write_rate`, and `cache_read_rate` (all cost per 1M tokens). The token table uses an adaptive column layout — 6 columns when no rows have cache activity, 8 columns when any row does.

`pricing.compute_totals()` is the single source of truth for aggregate figures (total tokens, total cost, the `any_unknown` flag for unpriced models, agent count, unique model names): `emit_token_table()`'s Total row and `review/reporting.py`'s compact usage line (`build_token_usage_line`) and high-usage warning (`build_high_usage_warning`) all derive from the same call, so the full table and the comment's compact summary can never report different numbers for the same run (#758). `review/reporting.py`'s `_prepare()` is the shared fail-soft setup (token-log assembly + pricing-file load) behind `build_token_table_accordion`, `build_full_token_table` (bare table, no `<details>` wrapper — used for the CI job-log echo), and `compute_token_totals`.

## Prompt caching

### Anthropic / Bedrock

When `AI_PROVIDER` is `anthropic` or `bedrock-proxy`, the LLM client uses Anthropic's ephemeral cache (5-minute TTL) via `cache_control: {type: "ephemeral"}` markers. Enabled by `LLM_PROMPT_CACHING` (default: `auto`):

- `auto` — enabled for `anthropic` and `bedrock-proxy`; no-op for OpenAI and Google Gemini.
- `true` — force-enable markers.
- `false` — force-disable; falls back to the legacy request layout.

#### Cache layout (`ai_pr_review/llm/anthropic.py:_build_body`, current as of #816)

Anthropic allows up to 4 `cache_control` breakpoints per request; the diff/user message always claims one of them, leaving at most 3 for `system` content. `_build_body` picks one of four layouts depending on what the caller (`agents/dispatch.py`) populated on the `LLMRequest`:

1. **N-block layout — caching enabled, `cache_blocks` non-empty (preferred).** Each non-empty entry in `LLMRequest.cache_blocks` (see [Shared run-context assembly](#shared-run-context-assembly): the PR-context block, then language profiles, then the feedback addendum, most-stable-first) gets its own `system` block with its own `cache_control` breakpoint, followed by a final unmarked block holding the per-agent `system_prompt`. The diff (`user_message`) gets the 4th breakpoint:
   ```
   system: [
     {type:"text", text:<cache_blocks[0]>, cache_control:{type:"ephemeral"}},
     {type:"text", text:<cache_blocks[1]>, cache_control:{type:"ephemeral"}},
     {type:"text", text:<cache_blocks[2]>, cache_control:{type:"ephemeral"}},
     {type:"text", text:<system_prompt>}
   ]
   messages: [{role:"user", content:[{type:"text", text:<user_message>, cache_control:{type:"ephemeral"}}]}]
   ```
   Because Anthropic's cache-hit check is prefix-cumulative (breakpoint N covers everything from the start of `system` through breakpoint N), ordering the most byte-stable fragment first means its own cache entry survives churn in a less-stable fragment later in the list, instead of one change invalidating everything after it the way a single joined block would. A run with only 1 or 2 populated fragments gets 1 or 2 breakpoints, not 3 padded ones; a hypothetical 4th fragment would be folded into the last block rather than exceeding the 4-breakpoint total.
2. **Two-breakpoint legacy layout — caching enabled, `cache_blocks` empty but `system_prefix` non-empty.** The whole run-shared system tail caches as ONE block ahead of `system_prompt`; preserved for any caller that populates `system_prefix` without `cache_blocks`.
3. **Single-breakpoint legacy layout — caching enabled, both empty.** Preserved for backward compatibility: `system: [{user_message, cache_control}, {system_prompt}]`, with `messages` reduced to a plain sentinel user turn.
4. **Caching disabled.** `system_prefix` (if any) and `system_prompt` are concatenated into a single plain string; no cache_control markers anywhere.

`build_body_for_bedrock` in the same module reuses `_build_body` for Bedrock's Anthropic-shaped request (model in the URL rather than the body), so Bedrock gets the identical layout logic.

Every agent that is `context_enrichment_eligible` (all finding agents except `blind-hunter`) receives the same `cache_blocks` tuple in the same run, so their shared fragments are cached once across the whole tiered fan-out rather than per-agent — there is no separate "cache cohort" concept; see [Shared run-context assembly](#shared-run-context-assembly).

#### Historical: live-benchmarked impact (issue #142, pre-#816 two-cohort layout)

**This table describes a layout that no longer exists.** It measured the original two-cache-cohort design (issue #142, before #816 replaced it with the N-block `cache_blocks` layout above) and is kept only as a historical data point for why caching was adopted at all — do not read it as a current-layout measurement, and do not extrapolate its percentages to the current 3-breakpoint design without re-benchmarking.

| Run (Sonnet 4.6, 5 agents, ~25 KB shared context) | input | cache_write | cache_read | est. cost | vs no cache |
|---|---:|---:|---:|---:|---:|
| A (caching off) | 56,652 | 0 | 0 | $0.189 | baseline |
| B (cold cache, first run) | 13,722 | 8,593 | 34,372 | $0.103 | **-46%** |
| C (hot cache, re-run within 5 min) | 13,722 | 0 | 42,965 | $0.073 | **-61%** |

No equivalent measurement has been taken against the current N-block layout as of this writing.

#### Cache priming (issues #144, #153) — removed, `AI_CACHE_PRIMING` is now a deprecated no-op

`AI_CACHE_PRIMING=true` used to serialize 1-2 cache-writing calls before Tier 1 fan-out so remaining agents would hit a guaranteed-warm cache:

1. `code-reviewer` (Sonnet primer for the code context cohort) concurrently with
2. `security-reviewer` (Opus primer, pulled forward from Tier 2 in full mode)

Investigation (#153) concluded that opportunistic cache hits from the parallel fan-out are sufficient in normal environments, so the default stayed `false`:

- The 7-agent fan-out has ~100-500ms natural stagger between calls (different system-prompt sizes 5-35 KB, different model TTFT, HTTP connection-pool serialization), which is enough time for the first cache write to become visible before subsequent agents reach the Anthropic API.
- A single-sample benchmark (PR #137, ~1185 diff lines, 8 agents, Bedrock Sonnet/Opus proxy) showed **zero cost difference** vs unprimed, with priming adding **+30s wall-clock (+20%)** overhead from the serial barrier.
- Anthropic's cache becomes visible faster than worst-case documentation suggests; agents starting within a few hundred milliseconds typically see each other's cache writes.

The implementation (`cache_priming_effective()` and `DispatchContext.cache_priming_env`) was deleted as dead code in #807 (zero production callers at that point). `AI_CACHE_PRIMING` stays accepted as a documented no-op with a deprecation warning (retrofitted in #824, since #807's commit message promised this but never actually added the registry entry); formal removal is planned for v3.0.0. See [configuration reference](configuration.md) for `AI_CACHE_PRIMING`.

#### Semantic change

Every caching-enabled layout above moves shared context out of the final user turn and into `system` content blocks ahead of the per-agent `system_prompt` — a structural change from the disabled layout's plain `system_prompt` + `user_message` shape. No benchmark script or automated quality check verifies this is model-neutral in this repository today (an earlier reference to a `claude/bench-quality.sh` script did not resolve to any file in this checkout); treat the equivalence as an operating assumption carried from the original #142 change, not as something currently re-verified in CI. The layout is used only when prompt caching is active; `LLM_PROMPT_CACHING=false` preserves the legacy disabled shape.

#### Cache-minimum threshold

Anthropic caches only prefixes >= 1024 tokens (Sonnet/Opus) or >= 2048 tokens (Haiku). Contexts below ~8KB may silently not cache — verify via `cache_creation_input_tokens` > 0 in the first response.

### OpenAI automatic prefix caching

OpenAI provides automatic prefix caching (50% discount on cached input tokens) for prompts >= 1024 tokens. No explicit markers are needed. The OpenAI client extracts `usage.prompt_tokens_details.cached_tokens` from the response and reports it as `cache_read`. The `input` count is adjusted to exclude cached tokens (matching Anthropic's convention) so the cost formula works correctly across providers.

#### Shared-cache layout for OpenAI (issue #164)

The OpenAI request is restructured for first-party OpenAI (`AI_PROVIDER=openai`) to maximize the shared prefix OpenAI's automatic prefix caching can detect across agents in the same run. `ai_pr_review/llm/openai.py:_build_body` puts the diff (`user_message`) first in the `system` message — this is the text most agents in a run share byte-for-byte — followed by an `===AGENT_INSTRUCTIONS===` separator and the per-agent tail (`system_prefix` + `system_prompt`, concatenated since OpenAI has no multi-breakpoint system-array support). The user message becomes a minimal sentinel (`"Please perform your review now."`). This mirrors the intent of the Anthropic shared-cache layout (issue #142) via string concatenation rather than a content-block array. Gated on `LLM_PROMPT_CACHING` (`auto`/unset enables it; `false`/`0` disables it) — independent of `resolve_temperature`/other per-request knobs, since OpenAI's caching itself is automatic and needs no explicit marker.

`openai-compatible` endpoints keep the legacy layout (`system` = agent prompt, `user` = diff) and use `max_tokens` instead of `max_completion_tokens`, since third-party endpoints may have different caching behavior or none at all.

### Google Gemini

Gemini uses a different caching API. `LLM_PROMPT_CACHING` has no effect on Gemini requests. Implicit caching (`cachedContentTokenCount`) is extracted when present.

## Retry and resilience

The LLM client retries transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520-524) with exponential backoff and jitter.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RETRY_COUNT` | `3` | Number of retry attempts (set to 0 to disable) |
| `LLM_RETRY_BASE_DELAY` | `2` | Base delay in seconds (doubles each retry) |

The VCS provider HTTP layer wraps critical API calls with retry logic (3 retries, exponential backoff + jitter) across all three providers.

## Graceful failure handling

When an agent call fails, the dispatch layer:
1. Logs a WARNING with the failure type and last error message
2. Records the agent name as failed and continues to the next agent

After all agents complete, `ai_pr_review/review/outcome.py:classify_review_outcome()` classifies the run:
- If **all** finding agents failed, the review aborts with exit 1
- Otherwise, failed agents are tracked and reported in the summary comment
- Any failed agent forces `may_approve=False` and `incomplete=True`. When that overrides an otherwise-APPROVE-eligible outcome — zero findings with a failure (`risk="Unknown"`), or a non-empty finding set whose highest severity is only Medium or Low — the event downgrades from `APPROVE` to `COMMENT`. A Critical/High finding always yields `REQUEST_CHANGES` regardless of failures, since that was never going to approve anyway.

## Standalone review mode

`review-target: standalone` (`REVIEW_TARGET=standalone`) is accepted by the engine, but the only behavior it currently changes is disabling merge-commit filtering in diff computation (`ai_pr_review/diff/compute.py`). Posting findings as a GitHub/GitLab issue was part of the bash engine removed in v2.0.0 and has not been reimplemented in the Python engine — no code path in `ai_pr_review/` currently creates an issue. Tracked as a known gap; see the repository issue tracker before relying on `standalone` for anything beyond `pr` mode's default behavior. (This section was re-verified for #805/#824: `ReviewConfig.standalone_depth`, an int field once parsed alongside this mode and reserved for a deeper standalone-review scan that was never built, was removed as dead code in #824 — see the `STANDALONE_DEPTH` row in [Environment variable reference](#environment-variable-reference). The behavior described above, and this section itself, is otherwise unaffected and still accurate.)

## Multi-arch container image

The `Dockerfile` builds for linux/amd64 and linux/arm64. Each binary download uses a `case "${TARGETARCH}"` block with per-arch SHA256 checksums. pip-installed tools (ruff, semgrep, checkov) and composer-installed tools (phpcs, phpstan) are arch-neutral.

### Multi-stage layout

- **`builder`** — installs build-time tooling, downloads analyzer binaries, pip-installs ruff/semgrep/checkov, composer-installs phpcs/phpstan. Semgrep registry rulesets are **not** baked into the image (they are use-restricted under the Semgrep Rules License v1.0); the semgrep analyzer uses `--config=auto` to fetch rules at runtime instead. See [Third-party licenses](#third-party-licenses).
- **final stage** — slim runtime with `bash`, `ca-certificates`, `curl`, `git`, `jq`, `php-cli` + extensions, `python3`. Copies `/usr/local/bin` and `/usr/local/lib/python${PYTHON_VERSION}/dist-packages` (parameterized via `ARG PYTHON_VERSION`, default `3.14` to match Ubuntu 26.04) wholesale from the builder. The Python package and action assets are copied at the end so source-only changes don't invalidate heavy builder layers.

## Test architecture

Tests live in `tests/python/` and use pytest. Key test files:

| File | Covers |
|---|---|
| `test_runtime.py` | Assembly boundary: `build_review_runtime()`, `SkipPlan`, SARIF routing, provider factory seam |
| `test_orchestrate.py` | `run_review()` happy path, skip path, summary/findings failure, token table |
| `test_cli.py` | `run_compute()`, `compute` command, `parse_changed_files_payload()`, `AI_PR_REVIEW_SCRIPT_DIR` resolution |
| `test_cli_parse_command.py` | `parse-command` subcommand (#821): the unified Python job-router replacing three bash `case` statements |
| `test_cli_slash.py`, `test_cli_dismiss.py`, `test_cli_dismiss_inline.py`, `test_cli_feedback_context.py` | `slash` subcommand and its dismiss/dismiss-inline/feedback-context CLI paths |
| `test_cli_policy_gate.py` | `_post_policy_gate_check_run` — the `ai-pr-review/policy-gate` merge-gate check-run (see [docs/policy.md](policy.md)) |
| `test_config.py` | `ReviewConfig.from_env()`, `resolve_models()`, unknown-var detection, deprecation warnings (`_DEPRECATED_NOOP_AI_VARS`, `_DEPRECATED_NOOP_ENV_VARS`, `_DEPRECATED_ANALYZER_NAMES`) |
| `test_manifest.py` | `build_changed_files()`, `build_manifest_text()`, `parse_changed_files_payload()` (including None-entry guard) |
| `test_language_profiles.py` | `load_language_profiles()` happy path, OSError fail-soft, missing profile key |
| `test_suppress.py`, `test_findings.py` | Suppression pipeline and findings merge |
| `test_sarif.py`, `test_bridge.py` | SARIF parsing and static analyzer bridge |
| `test_telemetry.py`, `test_logging.py` | Telemetry sink dispatch and structured log formatting |
| `test_feedback_*.py` | Learning loop: inject, store, retention, models |
| `vcs/test_github_*.py`, `vcs/test_gitlab_*.py` | GitHub and GitLab provider unit tests, split by concern (canonical-review flow, findings, stale-thread handling, summary CRUD, paginated reviews, check-runs, etc.) rather than one file per provider |

Run with `pytest tests/python/ -q`.

## Environment variable reference

Variables consumed by the engine but not exposed as action inputs:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_DIFF_LINES` | `5000` | Maximum diff lines before skipping review (mapped from `max-diff-lines` action input) |
| `AI_TEMPERATURE` | `0.3` | Sampling temperature for LLM calls (clamped to [0, 2]) |
| `AI_PARALLEL` | `true` | Tiered parallel agent execution |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score for findings |
| `AI_MAX_INLINE` | `25` | Maximum inline review comments per run |
| `AI_MAX_TOKENS_PER_AGENT` | `32768` | Max output tokens per LLM agent call; clamped to [256, 65536] |
| `AI_ENABLE_SUGGESTIONS` | `true` | Enable "Apply suggestion" buttons (GitHub and GitLab; ignored on Bitbucket) |
| `LLM_PROMPT_CACHING` | `auto` | Anthropic/Bedrock prompt caching. Valid: `auto`, `true`, `false` |
| `AI_CACHE_PRIMING` | `false` | Deprecated, ignored (#824 audit of #807): the cache-priming serialization mechanism was deleted as dead code. No-op with a deprecation warning; rejected starting in v3.0.0. |
| `AI_JUDGE_PASS` | `true` | Run the cheap-model judge pass (Phase 2.75) after findings are extracted. Set to `false` to disable. |
| `AI_FAIL_ON_FINDINGS` | `false` | Exit code 2 when the review outcome is `REQUEST_CHANGES` or `COMMENT`. CI-gate use case. |
| `AI_ANALYZER_CONCURRENCY` | `4` | Maximum simultaneous native static-analyzer subprocesses. Forced to 1 when `AI_PARALLEL=false`. |
| `AI_ANALYZER_DIFF_SCOPE` | `cap` | How out-of-diff native-analyzer findings are handled. Valid: `cap`, `drop`, `off`. |
| `AI_ANALYZERS` / `AI_EXCLUDE_ANALYZERS` | `''` | Allowlist / denylist of static analyzer names. See [Static analyzers](static-analyzers.md). `docs-missing-check` is accepted in either list as a documented no-op (#815: removed from `ANALYZER_NAMES`, superseded by `docs-api-check`/`docs-ref-check`/`docs-drift-check`; never dispatches regardless of membership). |
| `AI_AGENTS` / `AI_EXCLUDE_AGENTS` | `''` | Allowlist / denylist of review agent names. See [Agents](agents.md). |
| `AI_PROFILE_MAX_TOKENS` | `4096` | Deprecated, ignored (#814): per-agent language-profile routing was removed; every eligible agent now receives the whole detected-language profile(s). Accepted as a no-op with a deprecation warning; will be rejected starting in v3.0.0. |
| `STANDALONE_DEPTH` | — (not `AI_`-prefixed) | Deprecated, ignored (#824): reserved for a standalone review mode that was documented but never implemented (#623); its last reader, `ReviewConfig.standalone_depth`, was removed in #824. Warns via a separate `_DEPRECATED_NOOP_ENV_VARS` registry (not `AI_`-prefixed, so it can't go through the `AI_*` unknown-var scan); rejected starting in v3.0.0. |
| `AI_CONTEXT_ENRICHMENT` | `true` (config default) | Inject tree-sitter `<symbol-context>` blocks into agent prompts. |
| `AI_CONTEXT_MAX_TOKENS` | `8192` | Token budget for the injected `<symbol-context>` block per agent call. |
| `AI_CONTEXT_LOOKUP_LINES` | `8` | Lines of surrounding context captured per symbol lookup. |
| `AI_CONTEXT_MAX_QUERIES` | `200` | Maximum symbol lookups per review run. |
| `AI_EXCLUDE_PATTERNS` / `AI_EXCLUDE_PATTERNS_MODE` | `''` / `append` | Extra glob patterns to exclude from the diff, and whether they `append` to or `replace` the built-in excludes. |
| `AI_LOG_FORMAT` / `AI_LOG_LEVEL` | `human` / `WARNING` | Structured logging output format and level. |
| `AI_TELEMETRY_ENABLED` / `AI_TELEMETRY_SINK` | `false` / `''` | Emit structured telemetry events to the given sink. |
| `VCS_PROVIDER` | `github` | Selects the VCS provider. Valid: `github`, `bitbucket`, `gitlab` |
| `BITBUCKET_EMAIL` | — | Bitbucket-only. Bot user email (Basic-auth username) |
| `BITBUCKET_API_TOKEN` | — | Bitbucket-only. API token (Basic-auth password) |
| `BITBUCKET_WORKSPACE` / `BITBUCKET_REPO_SLUG` | — | Bitbucket-only. Optional explicit override |
| `GITLAB_TOKEN` | — | GitLab-only. Access token with `api` scope; falls back to `CI_JOB_TOKEN` |
| `GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab-only. API base URL for self-hosted instances. A bare host without `/api/v4` is accepted and normalized automatically. |
| `GITLAB_PROJECT_ID` | — | GitLab-only. Numeric project ID |
| `GITLAB_MR_DIFF_BASE_SHA` | — | GitLab-only. Base SHA for inline discussion positions |
| `GITLAB_BOT_USERNAME` | — | GitLab-only. Bot username for stale thread resolution |

For the complete, always-current list of every `AI_*` variable, see `_KNOWN_AI_VARS` and `ReviewConfig.from_env()` in `ai_pr_review/config.py` — this table covers the ones most relevant to understanding the runtime, not a substitute for the source.

## Provider model defaults

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-5` | `claude-opus-5` |
| `openai` | `gpt-5.4-mini` | `gpt-5.4` |
| `openai-compatible` | (user-specified) | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-5` | `global.anthropic.claude-opus-4-7` |
