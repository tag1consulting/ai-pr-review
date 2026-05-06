---
layout: default
title: Features
nav_order: 3
render_with_liquid: false
---

# Features

## Code suggestions

Code suggestions are enabled by default. The review tool asks eligible LLM agents to emit concrete code fixes alongside their findings. Each fix is rendered as a ` ```suggestion ` block inside the inline review comment, which GitHub and GitLab display as an "Apply suggestion" button — the PR/MR author can accept the fix with one click.

> **New in v0.6.0:** Suggestions now work on GitLab MRs using GitLab's
> native ` ```suggestion:-N+0 ` syntax for multi-line replacements.
> Previously suggestions were GitHub-only. Requires GitLab 14.0+
> (when the suggestion fence syntax was introduced). The
> `enable-suggestions` flag (`true` by default) applies uniformly
> across all VCS providers — setting it to `false` disables suggestions
> on both GitHub and GitLab. Bitbucket always ignores suggestions
> regardless of this flag.

To disable suggestions, set `enable-suggestions: false`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@v0.6.0
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

**LLM retries**: Transient API failures (HTTP 429, 500, 502, 503) and transient curl errors (connection refused, timeout) are retried with exponential backoff and jitter. Controlled by the `retry-count` input (default: 3).

**Parallel execution**: Agents run in a tiered fan-out by default (Tier 1: up to 3 concurrent; Tier 2 full-mode: up to 5 concurrent). If your LLM provider's rate limits cannot sustain this throughput, set `parallel: false` to revert to sequential execution.

**GitHub API retries**: Critical GitHub API calls (posting reviews, comments) retry on 502, 503, 429, and ETIMEDOUT with fixed backoff.

**Truncation recovery**: When an LLM response is truncated (hit max tokens), the action attempts to salvage valid findings from the partial JSON rather than discarding the entire agent output.

## Token usage

After each review run, a collapsible **Token usage by agent** table is appended to the review comment showing:

| Column | Description |
|--------|-------------|
| Agent | Agent name |
| Model | Human-readable model name (e.g. "Sonnet 4.6") |
| Input | Input tokens consumed |
| Output | Output tokens generated |
| Total | Combined token count |
| Est. Cost | Estimated cost at public list prices |

Costs are calculated using public list prices and do not reflect enterprise discounts, committed use agreements, or proxy markups. The table is also written to the [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary) for easy access from the Actions run page.
