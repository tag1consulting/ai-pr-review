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

The `source` field is optional in agent output. If the field is absent, the findings extractor adds the agent name. Static analyzers set their own source values in their output projection.

The fields `suggested_code` and `start_line` are optional. Agents emit them when the `enable-suggestions` action input is `true` (the default). See [Code suggestions](#code-suggestions).

The pipeline removes findings with confidence below 75. It removes duplicates with proximity-based matching. Findings in the same file within 3 lines of each other join one cluster. The pipeline keeps the highest-severity finding of the cluster. The surviving finding carries a `sources` array that holds all sources from the cluster. When a finding has more than one source, the VCS provider renders the sorted sources as `[source1, source2]`.

## Runtime flow

The entrypoint is `ai_pr_review.cli:review`. The Click command parses flags, sets up logging, and calls `_run_review_async()`. That function does these steps:

1. Calls `build_review_runtime(config)` in `ai_pr_review/review/runtime.py`. The runtime layer does these steps:
   - Resolves provider model defaults (`ReviewConfig.resolve_models()`).
   - Builds the VCS provider with `provider_from_env`.
   - Fetches the last-reviewed SHA and computes the diff (`ai_pr_review/review/compute.py`).
   - Returns `SkipPlan` if compute reports no changes.
   - Loads the feedback store (`AI_FEEDBACK_LOOP=1`) into a feedback addendum.
   - Fetches the PR/MR title and description with `provider.get_pr_description()`. This step is fail-soft: if the result is missing or malformed, the engine omits it. The engine adds the result and the file manifest to the shared `<pr-context>` block (`ai_pr_review/review/pr_context.py:build_shared_context_block`, #813).
   - Resolves `.ai-pr-review/policy.yml` (with `.github/ai-pr-review/policy.yml` as the fallback) if present (`ai_pr_review/policy.py`). It merges the agent and analyzer allow and deny lists and the review-mode default of any matched route with the explicit config. An explicit input always wins. See [docs/policy.md](policy.md) for the policy-file format. This step also sets `policy_gate_required` and `policy_gate_satisfied` for the merge-gate check run of the CLI.
   - Detects the languages of the changed files. It loads the whole text of the profile for every detected language once with `load_language_profiles()`. The engine stores the joined markdown in `DispatchContext.language_profile_text`. Each agent dispatch then reads from memory and not from disk (#814). Every eligible agent gets the whole profile and not a subset for that agent. See [Shared run-context assembly](#shared-run-context-assembly).
   - Runs native analyzers, loads SARIF findings (from `config.sarif_paths`), loads suppression rules, evaluates gates, and builds the `DispatchContext` and `OrchestrationConfig`. The engine merges all pre-computed findings into `OrchestrationConfig.extra_findings`.
2. Runs `pr-summarizer` on first (non-incremental) reviews. Then runs `issue-linker` on first reviews in `full` mode when the VCS provider is GitHub. Both agents are fail-soft (see `ai_pr_review/review/preflight.py`).
3. If `AI_DRY_RUN=1`, stops after assembly and does not post.
4. Otherwise calls `orchestrate.run_review()`. This function dispatches the eligible agents, merges LLM findings with pre-computed findings from `extra_findings`, applies suppressions, classifies the outcome, and posts with the provider.

**Boundary:** `run_review()` reads no environment and builds no dependencies, so it is the core that tests can run alone. `build_review_runtime()` is the seam between env-driven configuration and pure orchestration. Tests for the assembly layer are in `tests/python/test_runtime.py`.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment in the PR/MR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). The VCS provider extracts it at the start of each run. Later pushes diff from that SHA to HEAD. At the end of each run, the engine advances the watermark. It posts the summary comment with the new HEAD SHA.

The VCS provider keeps at most one summary comment on the PR/MR. It deletes duplicates after each upsert. Duplicates can build up when two runs start at the same time. The cleanup is non-fatal: a failed DELETE logs a WARNING.

To force a full-PR diff for one run, add the `ai-review-rescan` label to the PR. The workflow sets `FORCE_FULL_DIFF=true` in the `env:` block. The engine then skips the last-reviewed SHA lookup and uses the full `origin/BASE_REF...HEAD_SHA` diff.

## Canonical-review reuse (GitHub)

`ai_pr_review/vcs/_canonical.py` holds pure classification logic (no I/O). `GitHubProvider.post_findings` (`ai_pr_review/vcs/github.py`) uses it. On a rerun, the provider decides between two actions. It can `PUT` the body of the existing "canonical" review, or it can `POST` a new review object.

**Canonical review**: `select_canonical(reviews)` returns the bot review with the highest `id` and a non-empty body. It picks from the output of `GitHubProvider._list_prior_bot_reviews()`, in any state. It skips reviews with an empty or whitespace-only body. GitHub creates one of these reviews (state `COMMENTED`) each time the bot replies to an inline comment with the REST replies endpoint. Examples are the severity-escalation notice, a recurred-finding reply, and a feedback acknowledgement. GitHub rejects a `PUT` of a body onto such a review with HTTP 422.

The write side, `ai_pr_review.slash.github_ops._record_verdict`, calls the same function. Both sides agree on the canonical review when they use the same review list. `_record_verdict` gets its list from `list_bot_reviews()`. That method has a looser contract than `_list_prior_bot_reviews()`:

- On a pagination error, it returns partial results and not `[]`.
- It does not filter out `PENDING` reviews.

Both methods share one paginated walk (`GitHubProvider._list_reviews_paginated(*, strict, states)`), so they cannot drift apart on pagination or error handling. The contracts stay different on purpose. The other callers of `list_bot_reviews()` need every review in any state and accept a partial fetch. These callers are the F-id classification and the PR-wide auto-approve check in `ai_pr_review.slash.github_ops`. The bot always submits its own reviews with an explicit `event`, so it never leaves a review in `PENDING`. A `PENDING` review, or a fetch error on one side only, can still make the two sides disagree about the canonical review. This is an accepted and documented tradeoff and not a bug.

**Markers** (`ai_pr_review/vcs/marker.py`):
- `ai-pr-review-id-map` maps a fingerprint to a stable `F<n>` ID. This feature does not change it.
- `ai-pr-review-verdicts` maps a fingerprint to `"dismissed"`, `"fixed"`, or `"recurred"`. Slash commands write it (`_record_verdict`) and `classify()` reads it. `"recurred"` is a tombstone. The code writes it in place of a deletion when a `"fixed"` finding comes back. `merge_verdicts()` joins the marker across every prior review body, not only the canonical body. A plain delete on the newest body would not remove the key from an older body in that join.
- `ai-pr-review-finding` is a per-inline-comment marker. It holds base64-encoded `{fp, cat, sev}`. Nothing else keeps the category or severity of a rendered comment. The category is never rendered. The severity is only re-parsable from the `**[Sev]**` header token. The marker uses base64 and not raw JSON because `suggested_code` goes into the same comment body without escaping, ahead of the marker. A raw JSON marker could be forged or duplicated inside a rendered code fence.

**Per-finding decision** (`classify()`). The function checks these cases in order:

1. An exact match, or a fuzzy match gated on category and severity, against a `"dismissed"` verdict: the finding is suppressed for good.
2. An exact match against a `"fixed"` verdict: the finding is `recurred`. The provider replies, calls `unresolveReviewThread`, and sets the verdict to `"recurred"`.
3. A fuzzy `(file, ±3 lines, compatible category)` match against an open thread that the bot owns: the finding is `escalate` (PATCH and reply) if the severity went up. Otherwise it is `update` (PATCH). If the match is an *outdated* thread, the finding is `new` instead. GitHub already hides an outdated thread in its UI.
4. Anything else: the finding is `new`.

`classify()` does not know two hints that `post_findings` adds to a `new` finding:

- A human resolved a thread with no verdict command and the exact finding came back (`find_resolved_match`, issue #779). The new comment gets a note that says so.
- This bot resolved a thread (`resolve_stale`) because the finding went away, and a similar finding is back (`find_regressed_match`, issue #1089). The match is `(file, ±3 lines, compatible category, same source)` against a thread with `resolved_by_bot is True` and no verdict. A suppression-check finding matches only a thread of its own kind. A rolled-up finding never matches. The new inline comment, or the body bullet, notes that a similar finding was resolved earlier by this review and is back, and names its `F<n>` when it has one. The wording does not claim a push. A rescan of one commit resolves a thread too, when the model leaves a finding out once. The slash commands resolve a thread with the `github-token` input, so such a thread may also have `resolved_by_bot`, depending on whose identity that token has. A thread with a verdict is skipped in either case. Nothing is reopened, suppressed, or written to the verdicts marker. Only GitHub fills `resolved_by_bot`, so GitLab and Bitbucket do not show this note. Known limit: GitHub reports an outdated thread with the line of the old file, so a push that moves the code by more than 3 lines is not matched.

The two notes never appear together. A finding that matches the exact #779 case does not also get the #1089 note.

If two different findings in one run both fuzzy-match the same open thread, `dedupe_thread_claims()` keeps the claim with the higher severity. It sets the other finding back to `new`. One `PATCH` or reply then cannot absorb two findings.

Thread ownership (`parse_prior_thread`, `resolve_stale`, `_dismiss_stale_reviews`) depends on the per-comment marker and the author login. The code first normalizes the login with `ai_pr_review.vcs._stale.graphql_bot_login()`. The GitHub GraphQL API reports the bot login without the `"[bot]"` suffix of the REST API (verified live against a real thread). A comparison of the raw `self.config.bot_login` value with it rejected every real thread (issue #717). The code normalizes the login and does not pass `bot_login=None`. `ai_pr_review.slash.github_ops` uses `bot_login=None` for GraphQL authors. The team chose that before it confirmed the exact format difference. The normalized check keeps the author check as a real second defense against a forged marker.

`post_findings` also records which threads it PATCHed (`update`/`escalate`) or reopened (`recurred`) in this run. It stores them in `GitHubProvider._kept_alive_thread_ids`, and `resolve_stale` skips them. Without this, the next `resolve_stale` call would resolve a thread that this feature kept open on purpose. `orchestrate.py` runs the two calls one after the other (issue #718).

**Fingerprint drift on the `update` path (issue #720).** The `update`/`escalate` fuzzy match is loose on purpose (same file, within `PROXIMITY_LINES`, compatible category). The exact `fingerprint()` of the matched finding therefore often differs from the fingerprint that the thread had when posted. The cause can be reworded text or a small line shift. `_apply_thread_update` (`github.py`) PATCHes the comment in place with the *new* fingerprint in its metadata marker. The visible `**[F<n>]**` token does not change, because it is `thread.finding_id`, carried forward as is. Two things became stale as a result:

- A person ran `/ai-pr-review dismiss` while the comment still showed its *old* fingerprint. A later run reached `_fuzzy_dismissed_match` with that verdict. `_find_thread_by_fingerprint(all_threads, old_fp)` then found nothing, because the marker of the thread now held the new fingerprint. With no thread to read severity and category from, the match stopped. The dismissed finding was posted again as `new`.
- The engine builds `id_map` (the fingerprint to `F<n>` map) once, before classification. A reworded finding under `update`/`escalate` got a *second*, new `F<n>` id. The id went into the id-map marker of the review body, but nothing rendered it. This wasted an id. It also broke the reverse lookup `fingerprint_for_finding_id` (`ai_pr_review/vcs/_finding_ids.py`, called by `ai_pr_review.slash.github_ops` since #849). The lookup could resolve the visible token to the wrong fingerprint, or to none.

Two changes close both problems. The fuzzy-match tolerance stays the same:
- `marker.build_inline_meta_marker` and `InlineMeta` have a new optional field `prior_fingerprints` (`"pfp"` in the encoded payload). It holds every fingerprint that the comment of a thread has carried, capped at 20 entries (`_MAX_PRIOR_FINGERPRINTS`). Before a PATCH replaces the fingerprint, `_apply_thread_update` fills the field with the current fingerprint of the thread and the earlier values. `PriorThread.prior_fingerprints` exposes the field. `_find_thread_by_fingerprint` checks it next to the current fingerprint. A verdict recorded against any past fingerprint of the thread then resolves back to it.
- `GitHubProvider.post_findings` points `id_map[fingerprint(finding)]` at the existing `finding_id` of the matched thread for every `update`/`escalate` classification. It does this after `classify()` and `dedupe_thread_claims()` run and before it renders the id-map marker. The id-map then never gets a second, hidden id for a thread that has one. A later dismiss resolves to the fingerprint that the comment shows.

**Posting decision** (`decide_action()`). The function returns `PUT` when all of these conditions are true:

- A canonical body exists and its state is not `DISMISSED`.
- The state of the canonical review matches the event of this run.
- The canonical body carries the review footer. This guards against an overwrite of a message for people, such as the one from `submit_approval`.
- No `new` finding landed an inline comment (`any_new_inline_eligible`). The caller computes this value from the findings that ended up in `inline_comments` after `partition_findings`. It does not use the raw `is_inline_eligible` value. A finding that `max_inline` moved to the body, or whose payload build failed, must not count as if it got an inline slot.
- No `new` finding has High or Critical severity, **unless its exact fingerprint is already in `known_fingerprints`**. `ai_pr_review.vcs._finding_ids.known_fingerprints(prior_bodies)` returns every fingerprint that some prior review body already shows.

The `known_fingerprints` exemption lets a persistent out-of-diff High or Critical finding stop forcing a fresh review after its first appearance (issue #719). The same is true for any PR over `max_inline`. The `PUT` still renders the finding in the body every time, so the code hides nothing when it skips a `POST` for content that a person has already seen.

In all other cases, the provider does a `POST` of a fresh review that carries only the `new` findings. The provider dismisses the prior `CHANGES_REQUESTED` review **after** it confirms that the fresh POST landed as a non-degraded `REQUEST_CHANGES` review. It never dismisses first. If the post then degraded to a `COMMENT` review or a plain issue comment, the PR would be left unblocked without a signal. The provider also dismisses the prior review only when that review has zero unresolved owned threads on an unbroken thread fetch. An incomplete or failed `fetch_review_threads()` call never counts as "zero unresolved threads". `_dismiss_stale_reviews` applies the same gate to the thread count. As a result, the slash-command PR-wide auto-approve check cannot be fooled by a dismissed review that is still active.

**Concurrency**: just before the `PUT` of the canonical body, `_try_put_canonical` fetches the state and body of the review again (`get_review_state_and_body`). It also fetches the current head SHA of the PR (`get_pr_head_sha`). If either value changed, the result depends on which one:

- If the state or body changed, the provider posts a fresh review.
- If the head advanced, the provider skips the write. A newer run already owns the canonical review.

**This guard covers only the body `PUT`.** The per-thread `PATCH`, reply, and `unresolveReviewThread` side effects have no such re-check. The dismiss call for the superseded review has none either. The guard narrows the race window and does not remove it, even for the write that it covers. Consumers that push often must also set a GitHub Actions `concurrency:` group keyed on the PR number (see `examples/workflows/pr-review.yml`).

Canonical-review `PUT` reuse is GitHub-only. GitLab has its own cross-run dedup. It reuses the same `classify()` and `PriorThread` shape (`parse_gitlab_prior_thread`, `gitlab.py:post_findings`) with an empty `verdicts` map, so only `new`, `update`, and `escalate` are reachable (issue #710). Bitbucket calls `classify()` in `bitbucket.py:post_findings` with `all_threads=[]`. Only `new`, `recurred`, and `suppressed` are reachable there.

**Shared CRUD/thread helpers (#822).** `ai_pr_review/vcs/_upsert.py` (`upsert_comment`, `advance_sha_marker`) and `ai_pr_review/vcs/_thread.py` (the `first_comment*` GraphQL accessors, `count_unresolved_owned_threads`) hold code that was copied before. They hold the find-or-create-then-update control flow of the summary-comment CRUD of all three providers. They also hold the GraphQL review-thread reads and the thread counting of `github.py` and `slash/github_ops.py`. The provider-specific parts stay in each provider module. These are the marker, body, and footer text, the HTTP verb, and the payload shape. Only the control flow that was byte-identical moved.

## Shared run-context assembly

Every finding-producing agent except `blind-hunter` receives up to three prompt fragments that all agents share in a run. `blind-hunter` is excluded on purpose, because it must reason about the diff with no project context (see `AgentSpec.context_enrichment_eligible=False`). `build_review_runtime()` builds the fragments once per run and passes them through `DispatchContext`:

- **`shared_context_block`** (#813): the PR/MR title and description, and the changed-files manifest. `ai_pr_review/review/pr_context.py:build_shared_context_block()` wraps them in a `<pr-context>` block. It gets the title and description from `provider.get_pr_description()`, cuts them to 4000 characters, and strips HTML-comment template boilerplate. The block is empty when the title, body, and manifest are all empty.
- **`language_profile_text`** (#814): the whole joined markdown of every language profile detected in the changed files. The engine loads it once with `load_language_profiles()`. The older selection of a subset for each agent (`ProfileRouter`, `language_profile_sections.py`) is retired. Every eligible agent now gets the same whole text.
- **`feedback_addendum`**: recent learnings from the feedback-loop store (`AI_FEEDBACK_LOOP=1`). It is the least stable of the three, because it changes as the store gets new entries between reruns.

`_run_single_agent` in `ai_pr_review/agents/dispatch.py` joins the fragments that are not empty. The order is most stable first: the context block, then the language profiles, then the feedback. The joined text goes into `LLMRequest.system_prefix` (for providers without multi-breakpoint caching). The fragments also go separately into `LLMRequest.cache_blocks`. This is an ordered tuple of up to 3 fragments, each with its own cache tag. The Anthropic and Bedrock client uses it (see [Prompt caching](#prompt-caching)).

The current code has no "context variant" or cache-cohort concept. Every eligible agent receives the same fragments in the same order. Only the system prompt of each agent differs. That prompt is the base prompt, governance, knowledge-cutoff, findings-trailer, and an optional suggestion-addendum (see [Code suggestions](#code-suggestions)).

## Parallel agent execution

By default, the engine runs all eligible agents at the same time, with a limit on the number in flight. The wall-clock time depends on the model and on the size of the diff.

To turn this off, set the `parallel: false` action input or `AI_PARALLEL=false`. The default is `true`.

> **Breaking change (direct-script users):** Before issue #73, the engine used `AI_PARALLEL=false` when the variable was unset, and `action.yml` used `true`. The default is now `true` in both invocation paths.

`orchestrate.run_review()` makes one `run_tier` call. It passes one filtered list that holds the eligible agents of both tiers (`filter_agents` in `review/runtime.py`). `run_tier` (`agents/dispatch.py`) starts all agents in one task group. An `anyio.CapacityLimiter` limits how many run at once. The limit is 4, or 1 when `AI_PARALLEL=false`. The code has no barrier between Tier 1 and Tier 2.

### Tier groupings

The `tier` field of an agent sets its eligibility group and its model. It does not set a dispatch phase.

| Tier | Agents | When |
|------|--------|------|
| Tier 1 | `pr-summarizer` (first run only, dispatched separately, see below), `code-reviewer`, `silent-failure-hunter` (conditional: `has_error_patterns` gate) | Always |
| Tier 1 (static analyzers, concurrent with the agents) | All native analyzers (`ai_pr_review/analyzers/`) | Always (no-op if the binary is absent) |
| Tier 2 | `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general`, and `issue-linker` (dispatched separately) | `review-mode: full` only |

**Model selection.** `_run_single_agent` in `ai_pr_review/agents/dispatch.py` uses the premium model only when `spec.tier == 2 AND mode == "full" AND premium_model is set`. Every Tier 1 agent (including `silent-failure-hunter`) always uses the standard model, in both `quick` and `full` mode. The provider content filter can block the premium call of a Tier 2 agent (`stop_reason=refusal`, exit code 3). The call then retries once on the standard model (`fallback_from_model`, #810). The agent does not lose its coverage.

The roster marks `pr-summarizer` and `issue-linker` as `separately_dispatched=True`, and they never go through `run_tier`. They build their own prompt and user message. `pr-summarizer` uses the manifest, the commit log, and the diff. `issue-linker` uses the list of open issues. `cli.py` calls them directly before the main dispatch (see [Runtime flow](#runtime-flow)).

## Multi-provider support (GitHub / Bitbucket Cloud / GitLab)

Since v0.2.0, the same container image runs PR/MR reviews on GitHub, Bitbucket Cloud, and GitLab. The `VCS_PROVIDER` environment variable selects the provider:

| `VCS_PROVIDER` | Provider | Python module |
|---|---|---|
| `github` (default) | GitHub | `ai_pr_review/vcs/github.py` |
| `bitbucket` | Bitbucket Cloud | `ai_pr_review/vcs/bitbucket.py` |
| `gitlab` | GitLab | `ai_pr_review/vcs/gitlab.py` |

`provider_from_env` resolves the provider once at startup. The engine passes it through `ReviewRuntime`. An invalid provider value fails fast with a clear error.

## Code suggestions

When `enable-suggestions` is `true` (the default), eligible LLM agents emit an optional `suggested_code` field. They can also emit an optional `start_line` for multi-line replacements. The VCS provider posting layer wraps these in a suggestion fence in the format of the provider.

**Eligible agents** (their system prompt gets `prompts/suggestion-addendum.md`): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`.

Not eligible: `architecture-reviewer`, `adversarial-general`, `pr-summarizer`. Static analyzers never emit suggestions.

**Prompt composition.** At runtime, the dispatch layer composes the base prompt with up to four shared trailers:
- `prompts/_governance.md`: the Three Laws of Robotics (First and Second binding, Third stated and rejected), then five operational rules. The rules are: drop self-refuting findings, set severity by harm, detect when a finding reinvents the wheel, verify before naming plus secret redaction, and obey recorded maintainer verdicts from the `<repo-feedback>` block. It applies to all 7 finding-producing agents (not `pr-summarizer`). It is always on and has no env var toggle.
- `prompts/_knowledge-cutoff.md`: a HARD CONSTRAINT block against version-existence hallucinations. It applies to all 7 finding-producing agents (not `pr-summarizer`).
- `prompts/_trailer-findings.md`: the `json-findings` schema instruction. It applies to all 7 finding-producing agents.
- `prompts/suggestion-addendum.md`: the "Apply suggestion" formatting. `AI_ENABLE_SUGGESTIONS` gates it. It applies only to the 5 eligible agents.

The composition order is: base prompt, governance, knowledge-cutoff, findings-trailer, and the optional suggestion-addendum. The order is deliberate, for Anthropic prompt-cache locality. The byte sequence `_knowledge-cutoff`, `_trailer-findings`, `suggestion-addendum` at the end stays the same.

**Validation guards** (applied in the VCS provider posting layer):
1. `start_line` must be a positive integer and <= `line`.
2. Multi-line ranges are capped at `MAX_SUGGESTION_RANGE=100` lines.
3. The provider rejects `suggested_code` that contains triple backticks (prevents a fence escape).
4. For multi-line suggestions, every line in `start_line..line` must be in the new-file side of the diff.
5. When a guard fails, the provider drops the suggestion and logs a WARNING. The finding still posts with its natural-language remediation.

**Body finding rendering.** A finding with `suggested_code` can go to the review body. The provider posting layer then renders the suggestion as a plain code fence inside the collapsible `<details>` accordion. The triple-backtick sanitization guard also applies to body suggestions.

**Bitbucket** does not render suggestion fences. **GitLab** supports suggestion fences in its native `suggestion` syntax.

## Suppressions

`config/suppressions.json` is a JSON array of suppression rules. The engine evaluates them against merged findings. All `match` fields are optional. A finding must satisfy every field that a rule sets (the fields are ANDed):

- `file`: a regex, matched case-insensitively with `re.search` against the `file` of the finding. A plain substring such as `docs/` also works, because `re.search` matches anywhere in the path.
- `pattern`: a regex, matched case-insensitively with `re.search` against the finding text and the remediation text, joined with a space.
- `line`: an exact integer match.
- `line_start` and `line_end`: a window of lines. The rule matches when the span of the finding (`start_line` to `line`, or the single `line`) overlaps the window. `line_start` alone means "from this line on". `line_end` alone means "up to this line". A finding with no line number never matches a rule that uses these keys. A rule with `line_start` greater than `line_end` never matches.
- `code`: the finding text starts with this prefix.

The trusted global config can hold a catch-all rule (a rule with no `match` constraint). The engine rejects catch-all rules in the local file, because pull requests control that file. See [Suppression rules](suppression.md) for the full schema.

An optional `verify` field starts a check before the engine suppresses a finding:

See the table in [Suppression rules](suppression.md) for the `verify` values, what each one extracts, and the registry endpoint that it checks.

If the check confirms that the version exists, the suppression stands. If the check cannot confirm it, the engine keeps the finding. This covers a lookup error and a version that is not found. The engine does not support private registries (GHCR, GCR, ECR).

Consuming repos can add **local suppressions** at `.ai-pr-review/suppressions.json` (with `.github/ai-pr-review/suppressions.json` as the fallback). The engine merges them with the global rules at runtime.

## LLM judge pass

The findings pipeline (extract, merge, suppress, diff-scope) produces a final candidate list. Then `ai_pr_review/findings/judge.py:judge_findings()` runs. This is Phase 2.75, and `AI_JUDGE_PASS` gates it (default `true`). It sends one compact call to the standard model. A cheap model returns a `keep` or `downrank` verdict for each finding.

- `downrank` is the only verdict besides `keep`. There is no `drop`. The judge never removes a finding. The design prefers a visible false positive to a silently dropped true positive. `downrank` lowers confidence by `JUDGE_DOWNRANK_AMOUNT` (15). It moves the finding to the review body (`demoted_to_body=True`) and away from an inline comment. Severity does not change, because downranking affects placement and not the assessed risk.
- The judge always keeps findings with `Finding.corroborated=True`, whatever its verdict. An LLM agent and a static analyzer both confirmed these findings (see `findings/provenance.py`). The merge sets the flag only when an analyzer member and an agent member of the cluster describe the same kind of defect: equal real categories, or one real category and `other`. Two `other` findings do not count. A `docs` finding, and a finding with `Finding.style_only` set (an analyzer sets it per rule for style findings), cluster only with style findings of their own category. An agent finding never carries the flag, so the merge compares the flag only between analyzer findings. An agent `lint` finding can join a style hit of the same category. A style finding near a security finding of another category neither joins it nor corroborates it. A `lint` finding that is not style only, such as ruff F821, is a real defect and still merges with the agent finding for it. `extract.py` resets `style_only` on agent output. One cheap-model call cannot override agreement from independent sources.
- The pass is always fail-soft. After any LLM error, parse error, timeout, or empty input, it returns the findings unchanged (still `keep`) and logs a WARNING. `JudgeResult` also carries the token usage of the pass (`input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`). The token usage table shows it next to the finding-producing agents (see below).

## Token usage and cost estimation

The engine adds up token counts for each agent across all LLM calls. For Google Gemini, `cache_read` reports `cachedContentTokenCount` when present. The engine adds thinking tokens (`thoughtsTokenCount`) to the output count, because Google bills them at the output rate. The engine sends `thinkingConfig.thinkingLevel: low` to Gemini 3 models other than Flash-Lite. The default level can use almost the whole output limit, which includes thinking. The canary observed 30,938 of 32,768 tokens on `tests/canary/stress_diff.txt`. See `resolve_gemini_thinking_level()` in `ai_pr_review/llm/_config.py`. For OpenAI, `completion_tokens_details.reasoning_tokens` is reported as thinking tokens for visibility only, because `completion_tokens` already includes reasoning tokens.

`config/model-pricing.json` maps model ID patterns to display names and per-token rates. Each entry has four rates: `input_rate`, `output_rate`, `cache_write_rate`, and `cache_read_rate` (all cost per 1M tokens). An entry can also have a `long_prompt` object. It has a `threshold` and the same four rates. When the prompt of one call is longer than the threshold, `token_cost_units()` uses the `long_prompt` rates instead. The prompt is the input, cache-write, and cache-read tokens together. Only Claude Haiku 5.5 has this object (threshold 100,000 tokens). If a `long_prompt` object has no positive `threshold`, `input_rate`, or `output_rate`, the model is unpriced and a warning is logged. The token table has an adaptive column layout. It has 6 columns when no row has cache activity, and 8 columns when any row has it.

`pricing.compute_totals()` is the single source of truth for the aggregate figures. These are total tokens, total cost, the `any_unknown` flag for unpriced models, the agent count, and the unique model names. These all use the same call:

- the Total row of `emit_token_table()`
- the compact usage line of `review/reporting.py` (`build_token_usage_line`)
- the high-usage warning (`build_high_usage_warning`)

The full table and the compact summary of the comment therefore never report different numbers for the same run (#758). `_prepare()` in `review/reporting.py` is the shared fail-soft setup (token-log assembly and pricing-file load). Three functions use it: `build_token_table_accordion`, `build_full_token_table` (a bare table with no `<details>` wrapper, used for the CI job-log echo), and `compute_token_totals`.

### Pre-flight cost ceiling (`ai_pr_review/review/cost_ceiling.py`, #24)

Before any agent runs, `build_review_runtime()` in `review/runtime.py` estimates the total LLM spend of the run. It always logs the estimate as a structured `COST_ESTIMATE` line, with or without a configured ceiling. `estimate_review_cost()` covers the roster that `run_tier` dispatches. `estimate_preflight_agent_cost()` covers the two preflight agents that run separately (`pr-summarizer`, `issue-linker`), when they will run in this review.

The estimate uses these rules:
- Input tokens come from character counts, not from a real provider tokenizer. `estimate_billed_tokens()` in `review/cost_ceiling.py` divides the character count by 1.8 and adds a 10% margin. The ratio comes from measurements in `tests/data/token_ratio_measurements.json`. `context.budget.estimate_tokens` keeps its own 4-characters-per-token default for the context-enrichment budget and does not feed the cost estimate.
- Output tokens use the full effective cap of each agent. This is an upper bound and not a prediction. Real spend is usually lower.
- A model with no pricing entry cannot be bounded by the ceiling. The engine leaves it out of the total and logs a warning. Then `enforce_cost_ceiling()` applies `AI_COST_CEILING_UNPRICED` (#977).

The `AI_COST_CEILING_UNPRICED` values work as follows:
- `block` raises `UnpricedModelCeiling`. This is a subclass of `CostCeilingExceeded`, so it takes the same skip-comment path and honors `AI_FAIL_ON_COST_CEILING`.
- `warn` lets the run continue. `build_review_runtime()` records the names from `unpriced_models()` on `ReviewRuntime.cost_ceiling_unenforced_models`. `cli.py` renders them with `build_cost_ceiling_notice()` into the same warning slot as the high-usage warning (independent of `token-usage-display`) and into the step summary.

An empty pricing file (`cost_ceiling_pricing_missing`) never blocks, because it would skip every review that has a ceiling.

**Context-window check (#1056).** After it logs `COST_ESTIMATE`, `build_review_runtime()` calls `check_context_window()`. The check runs whether or not a cost ceiling is set. For each agent in the estimate (including the preflight agents) it adds the estimated input tokens and the output cap. It compares the sum with `AI_CONTEXT_WINDOW_MARGIN` times the `context_window` of the model in `config/model-pricing.json`. A model with no `context_window`, and a margin of `0`, are not checked. When an agent does not fit, the check raises `ContextWindowExceeded`. This is not a subclass of `CostCeilingExceeded`, because the cause is the diff size and not the cost. `build_review_runtime()` turns it into a `SkipPlan(is_context_window_skip=True)` that goes through the same skip-comment path, with the same analyzer and SARIF findings. `AI_FAIL_ON_CONTEXT_WINDOW` (read in `cli.py`) makes that skip exit with code 2. `AI_FAIL_ON_COST_CEILING` does not affect it.

**Provider "prompt too long" rejection (#1056).** The estimate can be too low. The provider can then reject a prompt with an HTTP 400 after the call is made. `retry_post()` in `llm/_http.py` raises `LLMContextLengthError` (a subclass of `LLMError`) when the status is 400 and the body matches a verified text. Only the Anthropic text `prompt is too long` is matched. The OpenAI, Google and Bedrock texts are not matched, because their exact 400 messages are not confirmed. A 400 with any other text stays a plain `LLMError`. `call_llm()` maps `LLMContextLengthError` to exit code 4 (`EXIT_PROMPT_TOO_LONG`). `_run_single_agent` copies the code and the model id into `FailedAgent`. The content-filter fallback does not apply to code 4, because it only handles code 3.

If every LLM agent failed with code 4, `run_review()` takes the skip path after `run_tier()`. It calls `_post_skip()`, which the pre-flight skip also uses. The skip comment has its own reason text, with the agent, the model and the `AI_EXCLUDE_PATTERNS` hint. It also carries the analyzer and SARIF findings. The run does not post a summary or findings, does not advance the watermark, and does not resolve stale threads. `ReviewResult.context_window_rejected` is set. `cli.py` reads it next to `SkipPlan.is_context_window_skip`. When `AI_FAIL_ON_CONTEXT_WINDOW` is `true`, the exit code is 2 and the telemetry outcome is `skipped_context_window_failed`. The run does not post the policy gate check. If only some agents get code 4, the review goes on. The failed-agent note then reads `<agent> (prompt too long for the model)`. If the failures have mixed causes, the review also goes on. The outcome counts the failed agents, so it is incomplete and not approvable, and `AI_FAIL_ON_FINDINGS` exits with code 2.

When `AI_MAX_COST_USD` is set and the estimate exceeds it, `enforce_cost_ceiling()` raises `CostCeilingExceeded`. `build_review_runtime()` turns it into `SkipPlan(is_cost_ceiling_skip=True)`. This uses the same skip-comment mechanism as the max-diff-lines skip in `review/compute.py`. Native static analyzers and SARIF ingestion run *before* this check (#848). They make no billed call and do not affect the estimate, so a cost-ceiling skip no longer skips them.

**Posting the skip-time findings (#896).** The engine no longer discards those pre-computed analyzer and SARIF findings. The `SkipPlan` also carries them (`extra_findings`), plus the real diff, the suppression rules, and the diff-scope mode. The skip branch of `orchestrate.run_review()` can then run the same suppress, diff-scope, and rollup pipeline as a normal run. It runs in-process with no LLM call, because it skips agent dispatch and the judge pass. It renders the kept findings as a body-level list in the one skip comment (`post_skip_comment(reason, findings=kept)`).

This path bypasses `post_findings` on purpose:
- It assigns no F-IDs, so no slash command can target one of these findings.
- It does not advance the incremental watermark.
- It does not resolve stale threads.
- It posts no formal `APPROVE` or `REQUEST_CHANGES` event.

A skip therefore affects only one comment.

`AI_FAIL_ON_FINDINGS` still applies. It reads the own outcome classification of the skip (`ReviewOutcome.may_approve`), like the exit code of the non-skip path. It works independently of `AI_FAIL_ON_COST_CEILING`. A clean cost-ceiling skip exits 0, even with `fail-on-findings` set. A skip whose surviving findings would block approval exits 2, even if `fail-on-cost-ceiling` is unset. This path also honors `AI_DRY_RUN` and does not post.

`ReviewRuntime.pre_flight_cost_estimate_units` carries the total of the estimate (in `pricing.format_cost` units of `$0.0001`) to `cli.py`. When the run completes, `cli.py` logs a `COST_RECONCILE` line. It compares the estimate with the real measured spend (`pricing.compute_totals()`). This is the estimate-versus-actual feedback loop that did not exist before (#848).

## Prompt caching

### Anthropic / Bedrock

When `AI_PROVIDER` is `anthropic` or `bedrock-proxy`, the LLM client uses the ephemeral cache of Anthropic (5-minute TTL) with `cache_control: {type: "ephemeral"}` markers. `LLM_PROMPT_CACHING` enables it (default: `auto`):

- `auto`: enabled for `anthropic` and `bedrock-proxy`. No effect for OpenAI and Google Gemini.
- `true`: force-enable the markers.
- `false`: force-disable the markers. The client falls back to the legacy request layout.

#### Cache layout (`ai_pr_review/llm/anthropic.py:_build_body`, current as of #816)

Anthropic allows up to 4 `cache_control` breakpoints per request. The diff or user message always uses one, so at most 3 remain for `system` content. `_build_body` picks one of four layouts. The choice depends on what the caller (`agents/dispatch.py`) set on the `LLMRequest`:

1. **N-block layout.** Caching is enabled and `cache_blocks` is not empty. This layout is preferred. Each non-empty entry in `LLMRequest.cache_blocks` gets its own `system` block with its own `cache_control` breakpoint. See [Shared run-context assembly](#shared-run-context-assembly): the PR-context block comes first, then language profiles, then the feedback addendum (most stable first). A final block without a marker holds the per-agent `system_prompt`. The diff (`user_message`) gets the 4th breakpoint:
   ```
   system: [
     {type:"text", text:<cache_blocks[0]>, cache_control:{type:"ephemeral"}},
     {type:"text", text:<cache_blocks[1]>, cache_control:{type:"ephemeral"}},
     {type:"text", text:<cache_blocks[2]>, cache_control:{type:"ephemeral"}},
     {type:"text", text:<system_prompt>}
   ]
   messages: [{role:"user", content:[{type:"text", text:<user_message>, cache_control:{type:"ephemeral"}}]}]
   ```
   The cache-hit check of Anthropic is prefix-cumulative. Breakpoint N covers everything from the start of `system` through breakpoint N. If the most byte-stable fragment is first, its cache entry survives a change in a less stable fragment later in the list. With one joined block, one change would invalidate everything after it. A run with only 1 or 2 fragments gets 1 or 2 breakpoints, not 3. A hypothetical 4th fragment goes into the last block, so the total stays at 4 breakpoints.
2. **Two-breakpoint legacy layout.** Caching is enabled, `cache_blocks` is empty, and `system_prefix` is not empty. The whole shared system tail caches as ONE block ahead of `system_prompt`. The code keeps this layout for callers that set `system_prefix` without `cache_blocks`.
3. **Single-breakpoint legacy layout.** Caching is enabled and both are empty. The code keeps this layout for backward compatibility: `system: [{user_message, cache_control}, {system_prompt}]`. `messages` holds only a plain sentinel user turn.
4. **Caching disabled.** The client joins `system_prefix` (if any) and `system_prompt` into one plain string. No `cache_control` markers exist.

`build_body_for_bedrock` in the same module reuses `_build_body` for the Anthropic-shaped Bedrock request (the model is in the URL and not in the body). Bedrock therefore gets the same layout logic.

Every agent that is `context_enrichment_eligible` (all finding agents except `blind-hunter`) gets the same `cache_blocks` tuple in the same run. The agents cache their shared fragments once for the whole fan-out, and not once for each agent. There is no separate "cache cohort" concept (see [Shared run-context assembly](#shared-run-context-assembly)).

#### Historical: live-benchmarked impact (issue #142, pre-#816 two-cohort layout)

**This table describes a layout that no longer exists.** It measured the original two-cache-cohort design (issue #142). Issue #816 replaced that design with the N-block `cache_blocks` layout above. The table stays as a historical data point that shows why the project adopted caching. Do not read it as a measurement of the current layout. Do not apply its percentages to the current 3-breakpoint design without a new benchmark.

| Run (Sonnet 4.6, 5 agents, ~25 KB shared context) | input | cache_write | cache_read | est. cost | vs no cache |
|---|---:|---:|---:|---:|---:|
| A (caching off) | 56,652 | 0 | 0 | $0.189 | baseline |
| B (cold cache, first run) | 13,722 | 8,593 | 34,372 | $0.103 | **-46%** |
| C (hot cache, re-run within 5 min) | 13,722 | 0 | 42,965 | $0.073 | **-61%** |

No one has measured the current N-block layout as of this writing.

#### Cache priming (issues #144, #153): removed, `AI_CACHE_PRIMING` is now a deprecated no-op

`AI_CACHE_PRIMING=true` used to run 1-2 cache-writing calls one after the other before the Tier 1 fan-out. The remaining agents then hit a cache that was sure to be warm:

1. `code-reviewer` (Sonnet primer for the code context cohort) concurrently with
2. `security-reviewer` (Opus primer, pulled forward from Tier 2 in full mode)

Investigation #153 concluded that the cache hits from the parallel fan-out are enough in normal environments. The default stayed `false` for these reasons:

- The 7-agent fan-out has a natural stagger of about 100-500 ms between calls. The cause is different system-prompt sizes (5-35 KB), different model TTFT, and HTTP connection-pool serialization. This gives the first cache write enough time to become visible before the other agents reach the Anthropic API.
- A benchmark with one sample (PR #137, about 1185 diff lines, 8 agents, Bedrock Sonnet/Opus proxy) showed **zero cost difference** against no priming. Priming added **+30 s wall-clock time (+20%)** from the serial barrier.
- The cache of Anthropic becomes visible faster than the worst-case documentation says. Agents that start within a few hundred milliseconds usually see the cache writes of each other.

#807 deleted the implementation (`cache_priming_effective()` and `DispatchContext.cache_priming_env`) as dead code, because it had no production callers. `AI_CACHE_PRIMING` stays accepted as a documented no-op with a deprecation warning. #824 added the warning, because the commit message of #807 promised it but never added the registry entry. The plan is to remove it in v3.0.0. See the [configuration reference](configuration.md) for `AI_CACHE_PRIMING`.

#### Semantic change

Every layout with caching moves the shared context out of the final user turn. It goes into `system` content blocks ahead of the per-agent `system_prompt`. This is a structural change from the plain `system_prompt` plus `user_message` shape of the disabled layout. No benchmark script or automated quality check in this repository verifies that this is model-neutral. An earlier reference to a `claude/bench-quality.sh` script did not resolve to any file in this checkout. Treat the equivalence as an operating assumption from the original #142 change. CI does not currently re-verify it. The layout is used only when prompt caching is active. `LLM_PROMPT_CACHING=false` keeps the legacy disabled shape.

#### Cache-minimum threshold

Anthropic caches only prefixes of >= 1024 tokens (Sonnet/Opus) or >= 2048 tokens (Haiku). A context below about 8 KB may not cache, and no error shows it. To check, confirm that `cache_creation_input_tokens` is > 0 in the first response.

### OpenAI automatic prefix caching

OpenAI gives automatic prefix caching (50% discount on cached input tokens) for prompts of >= 1024 tokens. It needs no explicit markers. The OpenAI client reads `usage.prompt_tokens_details.cached_tokens` from the response and reports it as `cache_read`. The client subtracts cached tokens from the `input` count (the same convention as Anthropic), so the cost formula works across providers.

#### Shared-cache layout for OpenAI (issue #164)

For first-party OpenAI (`AI_PROVIDER=openai`), the engine restructures the request. This gives the automatic prefix caching of OpenAI the largest shared prefix across the agents of a run. `_build_body` in `ai_pr_review/llm/openai.py` puts the diff (`user_message`) first in the `system` message. Most agents in a run share this text byte for byte. An `===AGENT_INSTRUCTIONS===` separator follows. After it comes the per-agent tail: `system_prefix` plus `system_prompt`, joined as one string, because OpenAI has no multi-breakpoint system array. The user message becomes a minimal sentinel (`"Please perform your review now."`). This follows the intent of the Anthropic shared-cache layout (issue #142), but with string concatenation and not a content-block array. `LLM_PROMPT_CACHING` gates it (`auto` or unset enables it, `false` or `0` disables it). It does not depend on `resolve_temperature` or other per-request settings, because OpenAI caching is automatic and needs no marker.

`openai-compatible` endpoints keep the legacy layout (`system` is the agent prompt, `user` is the diff). They use `max_tokens` and not `max_completion_tokens`, because third-party endpoints can have different caching behavior or none.

### Google Gemini

Gemini uses a different caching API. `LLM_PROMPT_CACHING` has no effect on Gemini requests. The client reads implicit caching (`cachedContentTokenCount`) when present.

## Retry and resilience

The LLM client retries transient API failures with exponential backoff and jitter. These are HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520-524.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RETRY_COUNT` | `3` | Number of retry attempts (set to 0 to disable) |
| `LLM_RETRY_BASE_DELAY` | `2` | Base delay in seconds (doubles each retry) |

The VCS provider HTTP layer wraps critical API calls with retry logic (up to 3 attempts in total, exponential backoff and jitter) for all three providers.

## Graceful failure handling

When an agent call fails, the dispatch layer does these steps:
1. Logs a WARNING with the failure type and the last error message.
2. Records the agent name as failed and continues with the other agents.

After all agents finish, `classify_review_outcome()` in `ai_pr_review/review/outcome.py` classifies the run:
- The summary comment lists the failed agents.
- A failed agent forces `may_approve=False` and `incomplete=True`.
- If every agent failed and there are no findings, the outcome is `event="COMMENT"`, `incomplete=True`, and `risk="Unknown"`. The review does not abort. Exit code 1 occurs only for configuration or posting errors.
- A failure can override an outcome that was eligible for `APPROVE`. This happens with zero findings and a failure (`risk="Unknown"`), or with findings whose highest severity is only Medium or Low. The event then changes from `APPROVE` to `COMMENT`.
- A Critical or High finding always gives `REQUEST_CHANGES`, whatever the failures. That outcome was never going to approve.

Before the result reaches a VCS provider, `cap_review_outcome()` processes it (issue #858). Both production call sites in `orchestrate.py` do this. `AI_APPROVAL_CEILING` (default `approve`, which has no effect) limits `event` to at most `REQUEST_CHANGES` or `COMMENT`. It never changes `may_approve`, `risk`, `incomplete`, or `finding_total`. The `AI_FAIL_ON_FINDINGS` exit-code check in `cli.py` reads `may_approve` and not `event`. A configured ceiling therefore changes what the engine *posts*, and does not change the CI exit code. See [Configuration: Approval ceiling](configuration.md#approval-ceiling).

## Standalone review mode

The engine accepts `review-target: standalone` (`REVIEW_TARGET=standalone`). Its only current effect is to disable merge-commit filtering in diff computation (`ai_pr_review/diff/compute.py`). The bash engine, removed in v2.0.0, could post findings as a GitHub/GitLab issue. The Python engine does not do this. No code path in `ai_pr_review/` creates an issue. This is a known gap. Check the repository issue tracker before you rely on `standalone` for anything beyond the default behavior of `pr` mode.

#805 and #824 re-verified this section. #824 removed `ReviewConfig.standalone_depth` as dead code. This was an int field that the code once parsed with this mode. It was reserved for a deeper standalone-review scan that no one built. See the `STANDALONE_DEPTH` row in the [Environment variable reference](#environment-variable-reference). The behavior described above is otherwise unchanged and still accurate.

## Multi-arch container image

The `Dockerfile` builds for linux/amd64 and linux/arm64. Seven tools (shellcheck, trufflehog, golangci-lint, hadolint, kube-linter, tflint, and ruff) are copied from digest-pinned official images. Only `gh` is downloaded, and its download is checked with SHA256. The pip-installed tools (semgrep, checkov) and the composer-installed tools (phpcs, phpstan) are arch-neutral.

### Multi-stage layout

- **`builder`**: installs build-time tooling, copies analyzer binaries from digest-pinned images, downloads `gh`, pip-installs semgrep and checkov, and composer-installs phpcs and phpstan. The image does **not** include the Semgrep registry rulesets, because the Semgrep Rules License v1.0 restricts their use. The semgrep analyzer uses `--config=auto` to fetch rules at runtime. See [`THIRD-PARTY-LICENSES/`](https://github.com/tag1consulting/ai-pr-review/tree/main/THIRD-PARTY-LICENSES).
- **final stage**: a slim runtime with `bash`, `ca-certificates`, `curl`, `git`, `jq`, `php-cli` and extensions, and `python3`. It copies `/usr/local/bin` and `/usr/local/lib/python${PYTHON_VERSION}/dist-packages` from the builder as a whole. `ARG PYTHON_VERSION` sets the version (default `3.14`, to match Ubuntu 26.04). The Python package and action assets are copied last. A change to source only then does not invalidate the heavy builder layers.

## Test architecture

Tests live in `tests/python/` and use pytest. Key test files:

| File | Covers |
|---|---|
| `test_runtime.py` | Assembly boundary: `build_review_runtime()`, `SkipPlan`, SARIF routing, provider factory seam |
| `test_orchestrate.py` | `run_review()` happy path, skip path, summary/findings failure, token table |
| `test_cli.py` | `run_compute()`, `compute` command, `parse_changed_files_payload()`, `AI_PR_REVIEW_SCRIPT_DIR` resolution |
| `test_cli_parse_command.py` | `parse-command` subcommand (#821): the unified Python job router that replaced three inline `case` statements in the workflow |
| `test_cli_slash.py`, `test_cli_dismiss.py`, `test_cli_dismiss_inline.py`, `test_cli_feedback_context.py` | `slash` subcommand and its dismiss/dismiss-inline/feedback-context CLI paths |
| `test_cli_policy_gate.py` | `_post_policy_gate_check_run`, the `ai-pr-review/policy-gate` merge-gate check run (see [docs/policy.md](policy.md)) |
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
| `AI_PARALLEL` | `true` | Parallel agent execution (up to 4 agents at once, 1 when `false`) |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score for findings |
| `AI_MAX_INLINE` | `25` | Maximum inline review comments per run |
| `AI_MAX_TOKENS_PER_AGENT` | `32768` | Max output tokens per LLM agent call. The engine clamps it to [256, 65536]. `AI_MAX_TOKENS_<AGENT>` (for example, `AI_MAX_TOKENS_CODE_REVIEWER`) overrides it for one named agent. `_run_single_agent` in `dispatch.py`, and `run_summarizer` and `run_issue_linker` in `preflight.py`, apply it with `config.resolve_agent_max_tokens()` (#191). See [Configuration: Per-agent max-tokens overrides](configuration#per-agent-max-tokens-overrides-env-var-only) for the full variable list. |
| `AI_ENABLE_SUGGESTIONS` | `true` | Enable "Apply suggestion" buttons (GitHub and GitLab, ignored on Bitbucket) |
| `LLM_PROMPT_CACHING` | `auto` | Anthropic/Bedrock prompt caching. Valid: `auto`, `true`, `false` |
| `AI_CACHE_PRIMING` | `false` | Deprecated and ignored (#824 audit of #807). The engine deleted the cache-priming mechanism as dead code. The variable has no effect and logs a deprecation warning. The engine rejects it starting in v3.0.0. |
| `AI_JUDGE_PASS` | `true` | Run the cheap-model judge pass (Phase 2.75) after the engine extracts findings. Set to `false` to disable it. |
| `AI_FAIL_ON_FINDINGS` | `false` | Exit code 2 when the review outcome is `REQUEST_CHANGES` or `COMMENT`. CI-gate use case. |
| `AI_APPROVAL_CEILING` | `approve` | Caps the strongest review event this bot may post. Valid: `approve`, `request-changes`, `comment`. Also disables the `/ai-pr-review dismiss` auto-approve-on-dismiss escalation (#590) when not `approve`. Never affects `AI_FAIL_ON_FINDINGS`'s exit code. See [Configuration: Approval ceiling](configuration#approval-ceiling). |
| `AI_ANALYZER_CONCURRENCY` | `4` | Maximum simultaneous native static-analyzer subprocesses. Forced to 1 when `AI_PARALLEL=false`. |
| `AI_ANALYZER_DIFF_SCOPE` | `cap` | How out-of-diff native-analyzer findings are handled. Valid: `cap`, `drop`, `off`. |
| `AI_ANALYZERS` / `AI_EXCLUDE_ANALYZERS` | `''` | Allowlist and denylist of static analyzer names. See [Static analyzers](static-analyzers.md). Either list accepts `docs-missing-check` as a documented no-op. #815 removed it from `ANALYZER_NAMES`. `docs-api-check`, `docs-ref-check`, and `docs-drift-check` replace it. It never dispatches, whether or not it is in a list. |
| `AI_AGENTS` / `AI_EXCLUDE_AGENTS` | `''` | Allowlist and denylist of review agent names. See [Agents](agents.md). |
| `AI_PROFILE_MAX_TOKENS` | `4096` | Deprecated and ignored (#814). The engine removed per-agent language-profile routing. Every eligible agent now gets the whole profile of each detected language. The engine accepts the variable, takes no action, and logs a deprecation warning. It rejects the variable starting in v3.0.0. |
| `STANDALONE_DEPTH` | none (no `AI_` prefix) | Deprecated and ignored (#824). It was reserved for a standalone review mode that the docs described but no one built (#623). #824 removed its last reader, `ReviewConfig.standalone_depth`. The engine warns about it through a separate `_DEPRECATED_NOOP_ENV_VARS` registry, because it has no `AI_` prefix and so cannot go through the `AI_*` unknown-variable scan. The engine rejects it starting in v3.0.0. |
| `AI_CONTEXT_ENRICHMENT` | `true` (config default) | Inject tree-sitter `<symbol-context>` blocks into agent prompts. |
| `AI_CONTEXT_MAX_TOKENS` | `8192` | Token budget for the injected `<symbol-context>` block per agent call. |
| `AI_CONTEXT_LOOKUP_LINES` | `8` | Lines of surrounding context captured per symbol lookup. |
| `AI_CONTEXT_MAX_QUERIES` | `200` | Maximum symbol lookups per review run. |
| `AI_EXCLUDE_PATTERNS` / `AI_EXCLUDE_PATTERNS_MODE` | `''` / `append` | Extra glob patterns to exclude from the diff, and whether they `append` to or `replace` the built-in excludes. |
| `AI_LOG_FORMAT` / `AI_LOG_LEVEL` | `human` / `WARNING` | Structured logging output format and level. |
| `AI_TELEMETRY_ENABLED` / `AI_TELEMETRY_SINK` | `false` / `''` | Emit structured telemetry events to the given sink. |
| `VCS_PROVIDER` | `github` | Selects the VCS provider. Valid: `github`, `bitbucket`, `gitlab` |
| `BITBUCKET_EMAIL` | none | Bitbucket-only. Bot user email (Basic-auth username) |
| `BITBUCKET_API_TOKEN` | none | Bitbucket-only. API token (Basic-auth password) |
| `BITBUCKET_WORKSPACE` / `BITBUCKET_REPO_SLUG` | none | Bitbucket-only. Optional explicit override |
| `GITLAB_TOKEN` | none | GitLab-only. Access token with `api` scope. If it is not set, the engine uses `CI_JOB_TOKEN` |
| `GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab-only. API base URL for self-hosted instances. A bare host without `/api/v4` is accepted and normalized automatically. |
| `GITLAB_PROJECT_ID` | none | GitLab-only. Numeric project ID |
| `GITLAB_MR_DIFF_BASE_SHA` | none | GitLab-only. Base SHA for inline discussion positions |
| `GITLAB_BOT_USERNAME` | none | GitLab-only. Bot username for stale thread resolution |

For the complete and current list of every `AI_*` variable, see `_KNOWN_AI_VARS` and `ReviewConfig.from_env()` in `ai_pr_review/config.py`. This table covers the variables most relevant to the runtime. It does not replace the source.

## Provider model defaults

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-5-5` | `claude-opus-5-5` |
| `openai` | `gpt-6-luna` | `gpt-6.1-sol` |
| `openai-compatible` | (user-specified) | same as standard |
| `google` | `gemini-3.5-flash-lite` | `gemini-3.8-flash` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-5` | `global.anthropic.claude-opus-4-7` |
