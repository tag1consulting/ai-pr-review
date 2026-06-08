---
layout: default
title: Features
nav_order: 3
render_with_liquid: false
---

# Features

## What's new in v1.3.0

**Concurrent native analyzer wrappers (PR #454, closes #354).** Native static-analyzer subprocesses (`shellcheck`, `trufflehog`, `semgrep`, `ruff`, and others) previously ran sequentially. They now run concurrently via `anyio.to_thread.run_sync` under a shared `CapacityLimiter`. Use the new `analyzer-concurrency` action input (or `AI_ANALYZER_CONCURRENCY` env var) to control the cap (default 4). Setting `parallel: false` forces the cap to 1 (restoring sequential behavior). Results are always returned in the original analyzer-list order for deterministic output. Python engine only. See [Configuration → analyzer-concurrency](configuration#static-analyzer-options).

**SARIF-equivalent skip for native wrappers (PR #454, closes #353).** When `AI_SARIF_PATHS` includes a SARIF file whose filename stem matches `ruff`, `semgrep`, or `hadolint` (case-insensitive), the corresponding native wrapper is suppressed and an INFO log is printed. This avoids running the same analyzer twice when you supply SARIF output from your own CI step. When `AI_SARIF_PATHS` is empty, behavior is unchanged. Python engine only.

**`AI_TEMPERATURE` is now honored in Python engine LLM requests (PR #453, closes #356).** The `temperature` action input (and `AI_TEMPERATURE` env var) was already validated but was never forwarded to the underlying LLM provider call. All agents, the pr-summarizer, and the issue-linker now receive the configured temperature. Default is 0.3 (unchanged). Python engine only.

**`max_tokens_per_agent` default lowered and clamped (PR #453, closes #357).** The default output-token budget per agent call is now **16384** (previously 32768 in the Python engine; docs and the bash engine said 8192 — now all three agree). Out-of-range values are clamped at config load time with a `WARNING` to stderr: below 256 is raised to 256, above 65536 is lowered to 65536. If you relied on the Python engine's prior 32768 default, add `max-tokens-per-agent: 32768` to restore it.

**`ignore-merge-commits` now defaults to `true` (PR #450, closes #448).** Merge commits that pull upstream base-branch changes into a PR are now excluded from the diff by default, so only the PR author's own commits are reviewed. **Breaking change**: if your PRs contain base-branch merge commits and you rely on them appearing in the diff, add `ignore-merge-commits: false` (or `AI_REVIEW_IGNORE_MERGE_COMMITS=false`) to restore the previous behavior.

**Context enrichment now defaults to `true` in the container image (PR #451, closes #391).** The container image ships `tree-sitter-language-pack` and `ripgrep`, so the dependencies required for context enrichment are always present. The `context-enrichment` input now defaults to `true` in `container-action/action.yml`. Direct-action consumers keep the `false` default.

**Issue-linker pre-fetches open issues via `gh issue list` (PR #447, closes #446).** The issue-linker agent now receives a pre-fetched list of open issues injected into its prompt, so it can resolve referenced `#N` numbers to real titles and surface genuinely related issues by keyword matching. Python engine only.

**Slash-command replies now post as `github-actions[bot]` (PR #452).** Slash-command and learning-loop replies previously posted as the PAT owner. They now post as `github-actions[bot]`. Callers of `slash-commands.yml` must add `actions-token: ${{ secrets.GITHUB_TOKEN }}` to the `secrets:` block. See [Slash commands](slash-commands.md) for the updated configuration.

## What's new in v1.2.0

**Diff-scope severity cap for native analyzer findings (PR #444, closes #359).** Native static analyzers (phpcs, phpstan, ruff, golangci-lint, semgrep, etc.) lint entire files — a single changed line in a large legacy file can produce hundreds of diagnostics on unchanged code. The new `analyzer-diff-scope` input (or `AI_ANALYZER_DIFF_SCOPE` env var) controls how those out-of-diff findings are handled. `cap` (default): downgrade out-of-diff analyzer findings to Low severity and collapse them into a `<details>` section in the review body — they remain visible but never trigger `REQUEST_CHANGES`. `drop`: remove them entirely. `off`: pass through unchanged (full-file linting behavior, pre-v1.2 default). LLM-agent findings are never affected regardless of this setting. Python engine only. See [Configuration → analyzer-diff-scope](configuration#static-analyzer-options).

**`exclude-patterns-mode` validation (PR #443, closes #442).** The `exclude-patterns-mode` input (and `AI_EXCLUDE_PATTERNS_MODE` env var) now validates that the value is `append` or `replace` — any other value raises an error at startup rather than silently falling through to append behavior. Values are case-insensitive (`APPEND`, `Replace`, etc. are all accepted and normalized to lowercase). Python engine only.

## What's new in v1.1.0

**Config-driven diff exclude patterns (PR #438, closes #436).** The diff exclude list is now configurable. Use the new `exclude-patterns` action input (or `AI_EXCLUDE_PATTERNS` env var) to supply comma-separated git pathspec glob patterns that are excluded from the diff before the LLM reads them — reducing token costs directly on repos with large generated, documentation-only, or vendored trees. The `":!"` pathspec prefix is added automatically. Default mode is `append`, which adds user patterns after the built-in lockfile/`vendor/`/`node_modules/` excludes; set `exclude-patterns-mode: replace` (or `AI_EXCLUDE_PATTERNS_MODE=replace`) to drop the built-ins entirely. Python engine only. See [docs/configuration.md](docs/configuration.md#diff-exclude-patterns).

**Line-range suppression rules (PR #439, closes #437).** Suppression rules now support `match.line_start` and `match.line_end` fields, scoping a rule to a specific line window within a file. This resolves the granularity gap for repos that vendor upstream code and apply patches: a rule can now target only the upstream line window (e.g. lines 1–200) so that findings on the user's own patched lines (201+) are never silenced. Multi-line findings match on overlap. A finding with no line number is never matched by a range rule. Python engine only. See [docs/suppression.md](docs/suppression.md).

## What's new in v1.0.2

**Slash-commands YAML parse fix (PR #434).** The Python heredoc in the slash-commands workflow was moved to an env block scalar, fixing a YAML parse error that prevented the workflow from running in some environments.

**Learning loop feedback context fix (PR #429).** The feedback store now correctly populates `source`, `file`, and `rule_id` fields for `issue_comment` events, so feedback entries written via `/ai-pr-review false-positive` and related commands carry the full context needed for relevance-ranked re-injection.

**Agent output token budget and ordering fix (PR #432).** Agent prompts now emit the `json-findings` block first (before prose explanations), reducing silent truncation on large diffs. The per-agent output budget was subsequently lowered to 16384 in v1.3.0; see the v1.3.0 notes above.

**E2E validation workflow (PR #433).** A Claude Code workflow (`ai-pr-review-e2e`) now builds the container image from the current checkout and runs live reviews against all three test platforms (GitHub, GitLab, Bitbucket) as part of the release process.

## What's new in v1.0.1

**Agent prompt parity with claude-comprehensive-review (PRs #414–#419).** Six agents — `pr-summarizer`, `edge-case-hunter`, `blind-hunter`, `adversarial-general`, `architecture-reviewer`, and `security-reviewer` — received targeted prompt improvements ported from the companion CCR plugin: tighter output structure, improved finding signal-to-noise, and better alignment with the shared language profiles.

**Analyzer correctness fixes (PRs #420–#423).** `run-semgrep.sh` gains stdin support and ruleset strategy documentation. `run-cve-check.sh` fixes range version truncation and `requirements.txt` pinning. `run-shellcheck.sh` and `run-trufflehog.sh` receive correctness improvements ported from CCR. All 12 analyzer wrappers now accept stdin input via the analyzer bridge.

**Learning loop data quality fix (PR #425).** `source`, `file`, and `rule_id` fields are now correctly populated in `learnings.jsonl` entries, making relevance-ranked feedback injection more accurate.

## What's new in v1.0.0

**Python engine is now the default.** `AI_PR_REVIEW_ENGINE` now defaults to `python`. Consumers who do not set `engine:` in their workflow will automatically use the Python engine. The bash pipeline is deprecated: it continues to work when explicitly set (`engine: bash` / `AI_PR_REVIEW_ENGINE=bash`) but emits a deprecation warning and will be removed in a future major release. To migrate, remove the `engine: bash` line from your workflow (or change it to `engine: python`). Context enrichment, SARIF ingestion, and the learning loop all require the Python engine and are unaffected — they remain opt-in via their existing env vars.

**Bash deprecation warning.** When `AI_PR_REVIEW_ENGINE=bash` is set explicitly, the bash entrypoint now emits a `::warning::` annotation in GitHub Actions (visible as a plain warning on GitLab/Bitbucket) reminding you to migrate. The review continues to completion — this is fail-soft.

## What's new in v0.12.2

**Third-party analyzer license compliance for the container image (PR #376).** The container image redistributes ~15 third-party open-source analyzers; this release adds a `THIRD-PARTY-LICENSES/` directory with each tool's full upstream license text and a `NOTICE.md` manifest (tool, version, license, copyright, corresponding-source URL), bundled into the image at `/opt/ai-pr-review/THIRD-PARTY-LICENSES/` and referenced from the README and architecture docs.

**Semgrep registry rulesets are no longer baked into the image (PR #376).** The Semgrep-maintained `p/ci` and `p/security-audit` rulesets are licensed under the Semgrep Rules License v1.0 (use-restricted; not freely redistributable inside this tool's image), so they are no longer pre-downloaded. `run-semgrep.sh` falls back to `--config=auto`, which fetches rules at runtime instead. **Behavior note:** semgrep scans now require network access at review time and re-incur the ~20–40s ruleset fetch that the bake step previously eliminated. Consumers who need an offline/deterministic ruleset can point `SEMGREP_RULES_DIR` at their own permissively-licensed rule bundle. Semgrep finding output is otherwise unchanged.

**`/ai-pr-review dismiss F<n>` now clears `CHANGES_REQUESTED` for inline findings too (PR #378).** Previously, dismissing an *inline* finding by its `F<n>` ID from a top-level PR comment stored the verdict but left the blocking review in place — it only told the user to go reply on the thread, and even manually resolving the thread did not clear the review (manual resolution does not re-trigger the workflow). Now the command locates the inline thread by its `[F<n>]` token, resolves it, and dismisses the `CHANGES_REQUESTED` review once every bot inline thread is resolved — the same outcome as replying directly on the thread. Thread ownership is gated by the `<!-- ai-pr-review-inline -->` marker rather than author login, which fixes a GraphQL/REST bot-login mismatch (`github-actions` vs `github-actions[bot]`) that would otherwise prevent the thread from being found.

## What's new in v0.12.1

**Removed sequence-diagram generation from `pr-summarizer` (PR #374).** The summarizer no longer produces an optional Mermaid `sequenceDiagram` block. GitHub does not render Mermaid in PR comments (only in `.md` files and PR description bodies), and Bitbucket does not render it at all, so the diagram was rarely seen where reviews are read, while adding LLM cost and prompt complexity. The summarizer continues to emit the summary, PR type, effort estimate, and walkthrough table. This is a behavior change to the summary comment but is not breaking for downstream consumers; no inputs or outputs that callers depend on were removed.

**Serena MCP onboarding config (PR #373).** Adds `.serena/` project onboarding configuration and memories for contributors using the Serena code-navigation MCP server. Purely additive: no runtime code paths change.

## What's new in v0.12.0

**Stable per-PR `F<n>` IDs on all findings, with `/ai-pr-review dismiss F<n>` from top-level PR comments (PRs #365, #366, #367, closes #364).** AI review findings now carry stable, monotonically increasing IDs — `**[F1]**`, `**[F2]**`, etc. — across both inline review-thread comments and body-level findings (those in the `### Findings not attached to specific lines` section). IDs are PR-wide: the same finding keeps its ID across review cycles, new findings get the next unused ID, and dismissed gaps (e.g. no `F2`) signal historical dismissals.

Before this release, `/ai-pr-review dismiss` silently did nothing when posted as a top-level PR comment, and body-level findings had no dismissal path at all. Now:

- `/ai-pr-review dismiss F1` posted as a **top-level PR comment** dismisses a specific body-level finding, records a `FeedbackEntry`, and auto-dismisses the `CHANGES_REQUESTED` review when all inline threads are also resolved.
- `/ai-pr-review dismiss` (no ID) replies with the list of active `F<n>` IDs instead of silently doing nothing — fixing the exact user-visible bug.
- Full parity: `false-positive F<n>`, `wont-fix F<n>`, `explain F<n>`, and `revise F<n>` all accept the same body-finding ID syntax from top-level comments.

The ID map is embedded as a hidden HTML comment in every review body (`<!-- ai-pr-review-id-map: {...} -->`) for stateless reconstruction without a side-channel database. A backward-compatible fallback parses rendered bullet text for pre-marker reviews.

**Incremental-run summary preservation (PR #366).** Incremental review runs (where the summarizer is skipped because the diff base matches the last-reviewed SHA) previously overwrote the existing summary comment with a bare `## AI Review` placeholder. The orchestrator now calls `advance_sha_watermark()` on incremental runs instead of `post_summary()`, preserving the original summary comment body while updating only the watermark SHA.

## What's new in v0.11.0

**Governance posture for LLM reviewers (PR #350).** A new shared prompt partial `prompts/_governance.md` is injected into all seven finding-producing agents (`code-reviewer`, `security-reviewer`, `architecture-reviewer`, `edge-case-hunter`, `blind-hunter`, `adversarial-general`, `silent-failure-hunter`). It encodes three principles: an Asimov-style severity lens (calibrate severity by harm to users/systems, not abstract "code smell"), don't-reinvent-the-wheel detection (flag duplication of existing utilities visible in the diff or manifest), and verify-before-naming with secret redaction (any flag/function/path named in a finding must appear in the supplied diff or manifest, and any secret-shaped value visible in the diff must be replaced with `<secret-redacted>` in finding and remediation text). Always-on, no env var toggle. Composition order is `base → _governance → _knowledge-cutoff → _trailer-findings → (suggestion-addendum)` to preserve prompt-cache locality.

**Telemetry schema v2 (PR #345, issues #242, #243).** The telemetry event payload bumps from schema version `"1"` to `"2"` with six additive fields: `provider`, `model_standard`, `model_premium`, `review_mode`, `is_incremental`, and `failed_agent_latency_ms`. The `outcome` enum gains `"skipped"` and `"dry_run"` values for runs where no agent dispatch occurs. All additions are forward-compatible — v1 consumers ignoring unknown keys continue to work; consumers switching on `telemetry_schema_version` should add v2 to their accepted list.

**GitHub Actions step summary (PR #345).** When `GITHUB_STEP_SUMMARY` is set (always true on GitHub-hosted runners), the Python engine now writes a concise markdown block to the step summary showing review mode, file/language counts, agent roster, findings tally by severity, failed agents, and the token cost table. Same layout as the PR comment, so operators see key metrics at a glance without opening the PR. Fail-soft — write errors are logged at WARNING and the review continues.

**Effective `max_tokens_per_agent` in cost table (PR #345).** When a user overrides the roster default via `AI_MAX_TOKENS_PER_AGENT`, the token cost table's Output column now displays the effective cap (e.g. `80 / 4096`) instead of the roster default (`80 / 16384`). Makes per-run token budgeting transparent.

**Dockerfile Python version centralized (PRs #340, #348, issue #340).** Hardcoded `python3.12` paths are replaced with `${PYTHON_VERSION}` interpolation driven by a single `ARG PYTHON_VERSION=3.12` declared in both build stages. Future Python bumps require changing one default instead of five hardcoded sites. No runtime behavior change for action consumers.

**Dependency updates.** Renovate updates: `ruff` 0.15.13 → 0.15.14 (#346), `ruby` 4.0.4 → 4.0.5 (#347).

## What's new in v0.10.1

**`max_tokens_per_agent` default corrected (PR #337, issue #334).** The Python engine's `config.py` defaulted to 4096 while `action.yml` documented 8192. Both now agree on 8192. Consumers relying on the Python engine default were getting half the intended token budget per agent.

**GitLab stale discussion resolution scoped to bot-owned discussions (PR #338, issue #184).** `resolve_stale_discussions` previously matched any discussion where the bot appeared as _any_ note author, including reply threads started by other users. It now only resolves discussions where the bot is the first-note author and the body contains the `<!-- ai-pr-review-inline -->` inline marker. The marker is appended to all newly posted inline discussions, so existing discussions are unaffected.

**Bash `dismiss_stale_reviews` hardened against silent failures (PR #342, issues #329, #325).** A jq parse failure on the GitHub reviews API response now emits a warning and returns early instead of silently continuing with an empty review ID list. An empty `newest_review_id` is similarly guarded.

**Python engine observability and correctness improvements (PR #341, issues #327–#333).**
- `ImportError` is no longer swallowed in the feedback loop and analyzer bridge fail-soft blocks — genuine import failures now propagate.
- `_safe_int` is no longer called twice per review ID in `github.py`'s bot review collection loop.
- SARIF load failure log now includes the file paths being loaded.
- Token table renderer failure log now includes `head_sha` for context.
- GitHub stale review dismissal now emits per-review-ID debug/warning log lines.
- Missing `language-profiles/` directory now logs a structured warning pointing to `AI_PR_REVIEW_SCRIPT_DIR` rather than returning silently.

**Language profiles loaded once per run (PR #343, issue #326).** The Python engine previously called `load_language_profiles()` on every agent dispatch. Profiles are now loaded once in `build_review_runtime()` and passed via `DispatchContext.language_profile_text`, eliminating redundant disk reads proportional to agent count.

## What's new in v0.10.0

**Language profiles — 19 languages (PR #322).** Agent prompts now include per-language context blocks for every language detected in the diff. Profiles cover Python, Go, TypeScript, JavaScript, PHP, Shell, Ruby, Rust, Java, C++, Kotlin, Swift, C#, Scala, SQL, Lua, Perl, YAML, and Terraform. Each profile supplies language-specific patterns, common pitfalls, and framework conventions so agents apply targeted checks rather than generic heuristics. Profiles are loaded from `language-profiles/` and injected into the `DispatchContext`; the bash engine reads them from the same directory via `build_file_manifest()`.

**Premature review dismissal fix (PR #324, issue #323).** Fixed a race condition where a stale `CHANGES_REQUESTED` review could be auto-dismissed before the current run finished posting its own review. Both the bash engine (`post-review.sh`) and the Python engine (`vcs/github.py`) now track the total bot-review count and always protect the newest bot review from dismissal. A `_safe_int()` helper guards against non-integer review IDs in both paths.

**Python engine runtime assembly refactor (PR #321).** `_run_review_async()` in `cli.py` has been reduced from ~230 lines to ~65 lines. A new `build_review_runtime()` factory in `ai_pr_review/review/runtime.py` assembles the fully prepared `ReviewRuntime` dataclass — provider construction, diff computation, feedback loading, agent gate evaluation, static analyzer runs, SARIF ingestion, suppression rule loading, and `OrchestrationConfig` construction — and hands it to `orchestrate.run_review()`, which reads no environment and constructs no dependencies. This makes the Python engine's runtime flow reusable for non-CLI entry points (server harness, batch runner). SARIF findings now flow through `OrchestrationConfig.extra_findings` rather than being loaded inline in the orchestrator.

## What's new in v0.9.4

**Token table moved to review body (`engine: python`).** The collapsible **Token usage by agent** table now appears in the same review comment as the findings (Approved / Changes Requested / Comment), matching the bash engine. Previously the Python engine appended the table to the long-lived PR summary comment, which was rewritten on every incremental run. The summary comment now carries only the first-run walkthrough and is never overwritten on subsequent pushes.

## What's new in v0.9.3

**Telemetry hooks.** Set `AI_TELEMETRY_ENABLED=true` and `AI_TELEMETRY_SINK=file:///path/to/events.jsonl` (or an `http(s)://` endpoint) to receive one structured JSON event per review run. The event includes outcome, findings counts by severity, per-agent token usage, per-agent wall-clock latency, SARIF elapsed time, and the count of learning-store entries loaded. Telemetry is fail-soft: all I/O errors are logged as warnings and the review continues. See [Configuration → Telemetry hooks](configuration#telemetry-hooks) for the full env-var reference.

**Agent latency tracking.** Each agent now records its wall-clock elapsed time (`elapsed_ms`). The value is included in the telemetry event's `agent_latency_ms` map and is available for future cost-table display.

**Token table enhancements.** The collapsible token cost table now includes two additional optional rows: a **Context enrichment** row showing the token count of the `<symbol-context>` block (when `AI_CONTEXT_ENRICHMENT=1` and context was non-empty), and a **SARIF ingestion** row showing the wall-clock parse time (when `AI_SARIF_PATHS` is configured). The Output column also displays the configured per-agent output cap when one is set (e.g. `1234 / 4096`).

**`cache_priming` default changed to `false`.** `AI_CACHE_PRIMING` now defaults to `false` in the Python engine, aligning with the bash engine default. Previously the Python engine defaulted to `true`. If you rely on cache priming, add `AI_CACHE_PRIMING=true` explicitly to your workflow.

**`slash-commands.yml` `shell: bash` fix.** The `feedback-command` job in the bundled slash-commands workflow was missing a `shell: bash` declaration, causing step failures on some runner configurations. This is fixed.

## What's new in v0.9.2

**Token cost table updated on every run (PR #304).** Previously (`engine: python`) the collapsible token cost table was only posted on the first review run and never refreshed. It now updates on every incremental run: the first-run PR summary text is preserved and only the `<details>` accordion is replaced with fresh token data from the latest run.

**Token table upsert bug fixes.** Fixed two bugs introduced in v0.9.1: (1) `_upsert_token_table` was a synchronous call inside an async function, blocking the anyio event loop during the GitHub API call; converted to `async def` with `anyio.to_thread.run_sync`. (2) HTTP-level errors from the VCS provider (403, 404, 422) were silently swallowed because providers return `SummaryResult(ok=False)` rather than raising — the return value is now checked and logged as a warning.

**`phpstan_level` default aligned to bash engine.** The Python engine was defaulting to `PHPSTAN_LEVEL=5`; the bash engine and documentation both specify `3`. Both are now `3`.

## What's new in v0.9.1

**Language detection expanded to 23 languages (PR #297).** The Python engine (`engine: python`) now detects Kotlin, Swift, C#, Scala, Terraform, YAML, SQL, Lua, Perl, plus Drupal PHP extensions (`.module`, `.theme`, `.inc`) and Ruby build files (`.rake`, `.gemspec`). Tree-sitter context enrichment (Capability A) covers all 23 language keys.

**PR summarizer and token cost table wired (PR #299).** On first-run reviews (`engine: python`), the engine now automatically posts a PR summary (walkthrough table, type classification, effort estimate) and a collapsible token cost table. Both are fail-soft: if either fails, review continues and a notice is posted rather than silently omitting output. The token cost table is updated on every run (see v0.9.2 below); the PR summary is posted on first run only.

**Structured logging (PR #300).** Set `AI_LOG_FORMAT=json` to get machine-readable log output with `timestamp`, `level`, `logger`, `correlation_id`, and `message` fields — suitable for Datadog, CloudWatch, and similar aggregators. Correlation IDs flow through every log record for the duration of a review run. Three-layer secret masking prevents credentials from appearing in log output. See [Configuration → Structured logging](configuration#structured-logging) for the full env-var reference.

**Error surface polish (PR #300).** All internal exceptions now use a typed hierarchy (`AiPrReviewError` → `ConfigError` / `ProviderError` / `CapabilityError` / `AnalyzerError` / `EngineError`). Warning messages follow a consistent `[ai-pr-review] WARNING: <component>: <message>` format across all modules.

## What's new in v0.9.0

**Python engine end-to-end.** `engine: python` now routes compute,
agent dispatch, and PR/MR posting through the Python implementation across
GitHub, GitLab, and Bitbucket. As of v1.0.0 the Python engine is the default;
the bash pipeline is deprecated.

**Three opt-in capability groups (all default off, all require the Python engine, which is the default since v1.0.0).** See
[Configuration → Opt-in capabilities](configuration#opt-in-capabilities)
for the full env-var reference.

**Capability A — Context enrichment** (default: `true` in the container image, `false` for direct-action consumers)
- Tree-sitter extracts symbol references from diff hunks (23 language keys), with a regex fallback when tree-sitter is unavailable.
- ripgrep looks up cross-file definitions and ranks by proximity (same-file > same-package > repo-wide).
- Definitions are token-budget-capped and injected into eligible agent prompts as a `<symbol-context>` block. Reduces hallucinated "should check X" findings.

**Capability B — SARIF 2.1.0 ingestion** (`AI_SARIF_PATHS=a.sarif,b.sarif`)
- Parse external scanner output (CodeQL, Semgrep, Trivy, Bandit, custom) into the existing finding pipeline.
- Severity mapping: `error → High`, `warning → Medium`, `note/none → Low`. Source tag: `sarif:<driver.name>`. Confidence: 90.
- Findings flow through the same dedup/suppress/post path as native analyzers. Fail-soft on malformed files.
- See [`examples/workflows/sarif-codeql.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/sarif-codeql.yml) for a CodeQL + AI review pipeline.

**Capability C — Learning loop** (`AI_FEEDBACK_LOOP=true`, GitHub-only)
- New slash commands: `/ai-pr-review false-positive [reason]`, `wont-fix [reason]`, `feedback <text>`, `explain`, `revise <hint>`.
- Verdicts persist to `.ai-pr-review/learnings.jsonl` on a dedicated `ai-pr-review-bot` branch (auto-bootstrapped on first write).
- Future reviews see a `<repo-feedback>` block of relevance-ranked recent entries, so repeated false positives get suppressed without further reviewer action.
- Security-hardened input pipeline: NFC normalization, control-char stripping, length cap, HTML escape, secret-pattern rejection, defensive prompt framing against injection.
- See [Learning loop](learning-loop) for the architecture.

## What's new in v0.7.0

**Performance**
- **Prompt caching** — Anthropic/Bedrock prompt caching via shared-cache layout delivers ~47% cost reduction on average (−46% cold, −61% hot). All agents in a cache cohort share one cache entry per run. (#137, #143)
- **Baked semgrep rulesets** — Container image ships with `p/ci` and `p/security-audit` rulesets pre-downloaded, eliminating the 20-40s network fetch on every run. (#136)
- **Analyzer overhead reduction** — Trufflehog uses batch invocation (single call for all files), checkov has tighter YAML/JSON content sniffing, phpstan avoids a subprocess for Drupal detection. (#134)

**Platform**
- **Multi-arch container images** — `linux/amd64` and `linux/arm64` builds, enabling native ARM runners. (#145)
- **Fork PR support** — Internal workflow uses `pull_request_target` for reviewing fork PRs. (#146)
- **Cache priming (opt-in)** — `AI_CACHE_PRIMING=true` serializes cache-writing calls before parallel fan-out for environments where opportunistic cache hits don't occur. Default off. (#154)

**Quality**
- **Prompt trailer consolidation** — Shared `_knowledge-cutoff.md` and `_trailer-findings.md` files replace duplicated blocks across 7 agent prompts, reducing maintenance surface. (#141)
- **Version hallucination hardening** — `ruby-org` verification type and portable ERE patterns in suppressions. (#140)

**Documentation**
- Comprehensive documentation audit addressing 27+ accuracy findings across README, CLAUDE.md, and the docs site. (#138, #155)

## Code suggestions

Code suggestions are enabled by default. The review tool asks eligible LLM agents to emit concrete code fixes alongside their findings. Each fix is rendered as a ` ```suggestion ` block inside the inline review comment, which GitHub and GitLab display as an "Apply suggestion" button — the PR/MR author can accept the fix with one click.

> **New in v0.6.0:** Suggestions now work on GitLab MRs using GitLab's
> native ` ```suggestion:-N+0 ` syntax for multi-line replacements.
> Previously suggestions were GitHub-only. Requires GitLab 11.6+
> (when the suggestion fence syntax was introduced). The
> `enable-suggestions` flag (`true` by default) applies uniformly
> across all VCS providers — setting it to `false` disables suggestions
> on both GitHub and GitLab. Bitbucket always ignores suggestions
> regardless of this flag.

To disable suggestions, set `enable-suggestions: false`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main  # or pin to a release tag
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    enable-suggestions: false
```

**Eligible agents** (those most likely to produce concrete line-level fixes): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`. Design-level agents (`architecture-reviewer`, `adversarial-general`) and static analyzers (shellcheck, semgrep, ruff, etc.) never emit suggestions.

**How it works.** Eligible agents have a short prompt addendum appended to their system prompt instructing them to include a `suggested_code` field (and optional `start_line` for multi-line replacements) only when the fix is concrete and complete. The post-review script constructs the suggestion fence itself — agents are not trusted to emit the markdown directly. Multi-line suggestions are validated against the diff: every line in the replacement range must appear on the new-file side of a diff hunk, or the suggestion is dropped while keeping the natural-language remediation.

**Caveats.** Suggestions increase output token usage. The feature works on both GitHub and GitLab (using GitLab's `suggestion` fence syntax) — Bitbucket reviews ignore it. Suggestions are validated defensively: `start_line` must be a positive integer no greater than `line` with no leading zeros, multi-line ranges are capped at 100 lines, and `suggested_code` containing triple backticks (which would break the suggestion fence) is rejected. When any validation fails, the suggestion is dropped with a WARNING logged to the Actions run and the finding still posts with its natural-language remediation. On incremental reviews (SHA watermark active), suggestions only render when the finding's line range is still in the current incremental diff — add the `ai-review-rescan` label to force a full re-review.

## Incremental reviews

After the first full-PR review, subsequent pushes trigger an incremental review that only analyzes the new commits. The SHA watermark is stored in the summary comment and advanced after each review run.

If the watermark cannot be found (e.g., the summary comment was deleted), the action falls back to a full PR diff.

To force a full-PR diff for a single run, add the **`ai-review-rescan`** label to the PR. The watermark still advances normally afterward, so subsequent pushes resume incremental review — re-add the label if you want another full rescan.

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, etc.), the review continues with the remaining agents and notes which agents were skipped. If all finding agents fail, the review is aborted.

**LLM retries**: Transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient curl errors (connection refused, timeout, network failure) are retried with exponential backoff and jitter. Controlled by the `LLM_RETRY_COUNT` env var (default: 3 for the bash engine, 2 for the Python engine).

**Parallel execution**: Agents run in a tiered fan-out by default — Tier 1 issues up to ~3 concurrent LLM calls alongside any triggered static analyzers; Tier 2 (full mode only) issues up to 5 concurrent LLM calls. The concurrency numbers apply to LLM calls only (for rate-limit planning); static analyzers run concurrently with them but do not consume LLM quota. If your provider's rate limits cannot sustain this throughput, set `parallel: false` to revert to sequential execution.

**GitHub API retries**: Critical GitHub API calls (posting reviews, comments) retry on 502, 503, 429, and ETIMEDOUT with fixed backoff.

**Truncation recovery**: When an LLM response is truncated (hit max tokens), the action attempts to salvage valid findings from the partial JSON rather than discarding the entire agent output.

## Token usage

After each review run, a collapsible **Token usage by agent** table is appended to the **review body** — the same comment that carries the findings (Approved / Changes Requested / Comment). Both the Python and bash engines now behave identically in this regard. The long-lived PR summary comment carries only the first-run walkthrough and is not rewritten on subsequent runs.

The table layout adapts based on cache activity:

| Column | Description | When shown |
|--------|-------------|------------|
| Agent | Agent name | Always |
| Model | Human-readable model name (e.g. "Sonnet 4.6") | Always |
| Input | Input tokens consumed | Always |
| Output | Output tokens generated; shown as `actual / cap` when a per-agent output cap is configured | Always |
| Cache Write | Tokens written to prompt cache | When any row has cache activity |
| Cache Read | Tokens read from prompt cache | When any row has cache activity |
| Total | Combined token count | Always |
| Est. Cost | Estimated cost at public list prices | Always |

When `LLM_PROMPT_CACHING` is active (default `auto` for Anthropic/Bedrock), the table expands to 8 columns showing Cache Write and Cache Read alongside the standard columns.

Two supplementary rows may appear after the **Total** row. They are informational only and do not affect cost totals:

| Row | Description | When shown |
|-----|-------------|------------|
| Context enrichment | Token count of the `<symbol-context>` block prepended to agent prompts | When `AI_CONTEXT_ENRICHMENT=1` and the enrichment block was non-empty |
| SARIF ingestion | Wall-clock elapsed time for parsing SARIF files (e.g. `0.34s`) | When `AI_SARIF_PATHS` is configured |

Costs are calculated using public list prices and do not reflect enterprise discounts, committed use agreements, or proxy markups. The table is also written to the [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary) for easy access from the Actions run page.
