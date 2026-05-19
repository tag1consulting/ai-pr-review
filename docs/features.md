---
layout: default
title: Features
nav_order: 3
render_with_liquid: false
---

# Features

## What's new in v0.9.2

**Token cost table updated on every run (PR #304).** Previously (`engine: python`) the collapsible token cost table was only posted on the first review run and never refreshed. It now updates on every incremental run: the first-run PR summary text is preserved and only the `<details>` accordion is replaced with fresh token data from the latest run.

**Token table upsert bug fixes.** Fixed two bugs introduced in v0.9.1: (1) `_upsert_token_table` was a synchronous call inside an async function, blocking the anyio event loop during the GitHub API call; converted to `async def` with `anyio.to_thread.run_sync`. (2) HTTP-level errors from the VCS provider (403, 404, 422) were silently swallowed because providers return `SummaryResult(ok=False)` rather than raising — the return value is now checked and logged as a warning.

**`phpstan_level` default aligned to bash engine.** The Python engine was defaulting to `PHPSTAN_LEVEL=5`; the bash engine and documentation both specify `3`. Both are now `3`.

## What's new in v0.9.1

**Language detection expanded to 23 languages (PR #297).** The Python engine (`engine: python`) now detects Kotlin, Swift, C#, Scala, Terraform, YAML, SQL, Lua, Perl, plus Drupal PHP extensions (`.module`, `.theme`, `.inc`) and Ruby build files (`.rake`, `.gemspec`). Tree-sitter context enrichment (Capability A) covers all 23 language keys.

**PR summarizer and token cost table wired (PR #299).** On first-run reviews (`engine: python`), the engine now automatically posts a PR summary (walkthrough table, type classification, effort estimate) and a collapsible token cost table. Both are fail-soft: if either fails, review continues and a notice is posted rather than silently omitting output. The token cost table is updated on every run (see v0.9.2 below); the PR summary is posted on first run only.

**Structured logging — Epic 4, Story 1 (PR #300).** Set `AI_LOG_FORMAT=json` to get machine-readable log output with `timestamp`, `level`, `logger`, `correlation_id`, and `message` fields — suitable for Datadog, CloudWatch, and similar aggregators. Correlation IDs flow through every log record for the duration of a review run. Three-layer secret masking prevents credentials from appearing in log output. See [Configuration → Structured logging](#structured-logging-epic-4) for the full env-var reference.

**Error surface polish — Epic 4, Story 5 (PR #300).** All internal exceptions now use a typed hierarchy (`AiPrReviewError` → `ConfigError` / `ProviderError` / `CapabilityError` / `AnalyzerError` / `EngineError`). Warning messages follow a consistent `[ai-pr-review] WARNING: <component>: <message>` format across all modules.

## What's new in v0.9.0

**Python engine end-to-end (Epic 2).** `engine: python` now routes compute,
agent dispatch, and PR/MR posting through the Python implementation across
GitHub, GitLab, and Bitbucket. The bash pipeline remains the default and is
unchanged. Required for all Epic 3 capabilities below.

**Epic 3 — three opt-in capability groups in the Python engine.** All default
off, all require `engine: python`. See
[Configuration → Opt-in capabilities](configuration#opt-in-capabilities-epic-3)
for the full env-var reference.

**Capability A — Context enrichment** (`AI_CONTEXT_ENRICHMENT=true`)
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

After each review run, a collapsible **Token usage by agent** table is appended to the summary comment (`engine: python`) or the review body (`engine: bash`). On the Python engine, the table is refreshed on every run — incremental reviews update the token data in place while preserving the first-run PR summary above it.

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
