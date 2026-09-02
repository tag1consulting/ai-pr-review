---
layout: default
title: Features
nav_order: 2
render_with_liquid: false
---

# Features

For what changed in each release, see [Version History](version-history).

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

## Quiet reruns (GitHub)

Rerunning the review on a PR no longer always creates a new top-level review object. Each run classifies its findings against the bot's most-recently-posted review (the "canonical" review, whichever state it's in — dismissed or not) and its existing threads:

- **Nothing new** (identical findings, no verdict changes): the canonical review's body is updated in place (`PUT`). No new Conversation-tab entry.
- **A still-open finding reworded or unchanged**: its comment is updated in place, silently. A severity *decrease* on a still-matched finding is also applied silently, with no reply — only an *increase* gets a notification (below); a downgrade with no accompanying explanation can look like the finding was tampered with rather than re-assessed, so treat a silent severity change on a rerun as a re-assessment, not a data-loss signal.
- **A still-open finding's severity increases**: its comment is updated in place, plus a reply on that thread noting the escalation. Still no new review.
- **A finding marked `/ai-pr-review fixed` reappears unchanged**: a reply explains it recurred and the thread is reopened. No new review.
- **A finding marked `/ai-pr-review dismiss`/`false-positive`/`wont-fix`**: never reposted, permanently — including a similar finding nearby, as long as it's a compatible category and no more severe than what was dismissed.
- **A genuinely new finding** (or one severe enough — High/Critical — that it must be visible even without a diff anchor): a fresh review is posted, carrying only the new finding(s). The prior blocking (`CHANGES_REQUESTED`) review is dismissed — **after** the fresh review is confirmed to have actually posted as a real, non-degraded blocking review, never before — only once none of its own findings are still open (and only when the thread fetch that determined that completed without error); it is never dismissed out from under an active finding, and a mid-run posting failure never leaves the PR silently unblocked.
- **A persistent finding with no diff anchor** (out-of-diff, or bumped to the body by `max-inline`): the first time it appears, it forces a fresh review like any other new High/Critical finding above. On every rerun after that, as long as its exact text/location is unchanged, it's already visible in the canonical review's body — so it no longer forces a fresh review on its own, and the existing body is updated in place instead. Only a change to the finding itself (different wording, a moved line) resets this and requires a fresh review again.

This is GitHub-only for now; GitLab and Bitbucket still post fresh review content on every run (tracked as a parity gap, issue #710). Set `AI_CANONICAL_REUSE=false` to disable reuse entirely and restore the pre-reuse behavior of always posting a fresh review — see [Configuration](configuration#quiet-reruns-github-only).

**Concurrency note**: if two runs land on the same PR close together (rapid pushes with no `concurrency:` group configured), there's a narrow window where one run's write to the canonical review can be superseded by the other's. The action re-checks the canonical review's state immediately before **the body `PUT`** and falls back to posting a fresh review if anything changed underneath it — this guard covers only that write; the per-thread comment updates, notification replies, and the dismiss-superseded-review call have no equivalent re-check, so this narrows the window rather than eliminating it. Configure a GitHub Actions `concurrency:` group keyed on the PR number if your repo pushes frequently enough for this to matter — see `examples/workflows/pr-review.yml` for the shipped example.

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, etc.), the review continues with the remaining agents and notes which agents were skipped. If all finding agents fail, the review is aborted.

**LLM retries**: Transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient network errors (connection refused, timeout) are retried with exponential backoff and jitter. Controlled by the `LLM_RETRY_COUNT` env var (default: 2).

**Parallel execution**: Agents run in a tiered fan-out by default — Tier 1 issues up to ~3 concurrent LLM calls alongside any triggered static analyzers; Tier 2 (full mode only) issues up to 5 concurrent LLM calls. The concurrency numbers apply to LLM calls only (for rate-limit planning); static analyzers run concurrently with them but do not consume LLM quota. If your provider's rate limits cannot sustain this throughput, set `parallel: false` to revert to sequential execution.

**GitHub API retries**: Critical GitHub API calls (posting reviews, comments) retry on 502, 503, 429, and ETIMEDOUT with fixed backoff.

**Truncation recovery**: When an LLM response is truncated (hit max tokens), the action attempts to salvage valid findings from the partial JSON rather than discarding the entire agent output.

## Token usage

After each review run, a collapsible **Token usage by agent** table is appended to the **review body** — the same comment that carries the findings (Approved / Changes Requested / Comment). The long-lived PR summary comment carries only the first-run walkthrough and is not rewritten on subsequent runs.

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

The `judge-pass` row appears as a regular agent row (included in the Total) when the judge actually ran:

| Row | Description | When shown |
|-----|-------------|------------|
| `judge-pass` | Tokens consumed by the judge-pass LLM call; included in Total | When `AI_JUDGE_PASS=true` (default) and the judge ran on a non-empty finding set |

Two supplementary rows may appear after the **Total** row. They are informational only and do not affect cost totals:

| Row | Description | When shown |
|-----|-------------|------------|
| Context enrichment | Token count of the `<symbol-context>` block prepended to agent prompts | When `AI_CONTEXT_ENRICHMENT=1` and the enrichment block was non-empty |
| Language profiles | Maximum profile tokens injected across all agents (per-agent routing, v2.1.0+) | When language profiles were injected and the count was non-zero |
| SARIF ingestion | Wall-clock elapsed time for parsing SARIF files (e.g. `0.34s`) | When `AI_SARIF_PATHS` is configured |

Costs are calculated using public list prices and do not reflect enterprise discounts, committed use agreements, or proxy markups. The table is also written to the [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary) for easy access from the Actions run page.
