---
layout: default
title: Features
nav_order: 2
render_with_liquid: false
---

# Features

For what changed in each release, see [Version History](version-history).

## Code suggestions

Code suggestions are on by default. The review tool asks eligible LLM agents to give concrete code fixes with their findings. The tool shows each fix as a ` ```suggestion ` block inside the inline review comment. GitHub and GitLab display that block as an "Apply suggestion" button, and the PR or MR author can accept the fix with one click.

> **New in v0.6.0:** Suggestions work on GitLab MRs. They use the native ` ```suggestion:-N+0 ` syntax of GitLab for multi-line replacements. Before this version, suggestions were GitHub-only. This needs GitLab 11.6 or later (the version that introduced the suggestion fence syntax). The `enable-suggestions` flag (`true` by default) applies to all VCS providers. If you set it to `false`, suggestions are off on both GitHub and GitLab. Bitbucket always ignores suggestions, whatever the flag value.

To turn off suggestions, set `enable-suggestions: false`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main  # or pin to a release tag
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    enable-suggestions: false
```

**Eligible agents** (the agents most likely to give concrete line-level fixes): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`. Design-level agents (`architecture-reviewer`, `adversarial-general`) and static analyzers (shellcheck, semgrep, ruff, and others) never give suggestions.

**How it works.** The tool adds a short prompt addendum to the system prompt of each eligible agent. The addendum tells the agent to include a `suggested_code` field (and an optional `start_line` for multi-line replacements) only when the fix is concrete and complete. The post-review code builds the suggestion fence itself. The tool does not trust agents to write the markdown directly. The tool checks multi-line suggestions against the diff. Every line in the replacement range must appear on the new-file side of a diff hunk. If not, the tool drops the suggestion and keeps the natural-language remediation.

**Caveats.** Suggestions increase output token usage. The feature works on GitHub and GitLab (it uses the `suggestion` fence syntax of GitLab). Bitbucket reviews ignore it. The tool checks each suggestion with strict rules:

- `start_line` must be a positive integer, no greater than `line`, with no leading zeros.
- Multi-line ranges have a limit of 100 lines.
- The tool rejects `suggested_code` that contains triple backticks, because they break the suggestion fence.

If a check fails, the tool drops the suggestion and writes a WARNING to the Actions run log. The finding still posts with its natural-language remediation. On incremental reviews (SHA watermark active), a suggestion shows only when the line range of the finding is still in the current incremental diff. To force a full re-review, add the `ai-review-rescan` label.

## Incremental reviews

After the first full-PR review, each later push starts an incremental review. The incremental review analyzes only the new commits. The tool stores the SHA watermark in the summary comment and advances it after each review run.

If the tool cannot find the watermark (for example, someone deleted the summary comment), the action uses the full PR diff.

To force a full-PR diff for one run, add the **`ai-review-rescan`** label to the PR. The watermark still advances as usual afterward, so later pushes resume incremental review. To get another full rescan, add the label again.

### A finding that comes back after a fix (GitHub)

When a later run no longer reports a finding, the review resolves its thread. If a similar finding then appears in the same code, the new comment says that a similar finding here was resolved earlier and is back. A finding counts as similar when it has the same file, a line within 3 lines, a compatible category, and the same source. The note only appears when the code at that place changed, because a thread of unchanged code is not resolved by a push. It does not claim that a push or a fix happened. A human resolution with no verdict gets a different note. A finding never gets both notes. A push that moves the code by more than 3 lines hides the match, so the note does not appear then.

## Quiet reruns (GitHub)

A rerun of the review on a PR does not always create a new top-level review object. Each run compares its findings with two things. The first is the most recent review that the bot posted with a body (the "canonical" review), in any state, dismissed or not. The second is the existing threads of that review. The tool never treats an empty-body review as canonical. GitHub creates such a review automatically when the bot replies to an inline comment, and GitHub rejects any update to its body. The outcomes are:

- **Nothing new** (identical findings, no verdict changes): the tool updates the body of the canonical review in place (`PUT`). The Conversation tab gets no new entry.
- **A still-open finding, reworded or unchanged**: the tool updates its comment in place, with no notification. A severity *decrease* on a still-matched finding is also silent, with no reply. Only an *increase* sends a notification (see below). A downgrade with no explanation can look like tampering. Treat a silent severity change on a rerun as a new assessment, not as lost data.
- **The severity of a still-open finding increases**: the tool updates its comment in place and adds a reply on that thread that notes the escalation. The tool does not post a new review.
- **A finding marked `/ai-pr-review fixed` reappears unchanged**: a reply explains that it recurred, and the tool reopens the thread. The tool does not post a new review.
- **A finding marked `/ai-pr-review dismiss`, `false-positive`, or `wont-fix`**: the tool never posts it again. This includes a similar finding nearby, if its category is compatible and it is not more severe than the dismissed finding.
- **A new finding** (or a High or Critical finding that must be visible even without a diff anchor): the tool posts a fresh review that carries only the new findings. The tool dismisses the prior blocking (`CHANGES_REQUESTED`) review only when all of these are true:
  - The fresh review is confirmed to have posted as a real, non-degraded blocking review. The tool never dismisses the prior review before this.
  - None of the findings of the prior review are still open.
  - The thread fetch that determined this completed without error.

  The tool never dismisses a review that has an active finding. A posting failure during a run never leaves the PR unblocked without notice.
- **A persistent finding with no diff anchor** (out of the diff, or moved to the body by `max-inline`): the first time it appears, it forces a fresh review, like any other new High or Critical finding above. On each later rerun, if its exact text and location are unchanged, the body of the canonical review already shows it. It then does not force a fresh review, and the tool updates the existing body in place. Only a change to the finding itself (different wording, a moved line) resets this and requires a fresh review again.

This full canonical-review reuse works on GitHub only. GitLab has a narrower version. `post_findings` fuzzy-matches each finding against the still-open prior discussions of this bot (same file, within 3 lines, compatible category). It does not repost an unchanged finding. It updates the note of the matched discussion in place instead. The severity, remediation, and wording all refresh, and a higher severity also gets a reply that notes the change. GitLab has no dismiss, false-positive, wont-fix, or fixed verdict system, so it has no suppression. A `resolve_stale` run never re-resolves a discussion that the same run created or matched. To restore the older GitLab behavior of a fresh discussion for every eligible finding on every run, set `AI_GITLAB_CROSS_RUN_DEDUP=false`.

Bitbucket dedup works in a different way. Bitbucket has no comment threads to fuzzy-match. Instead, each eligible finding appears as a Code Insights annotation, and the tool rebuilds all of them from the start on every run (DELETE, then PUT, then POST). A finding that someone dismissed through the hidden verdicts marker of the summary comment is not in that rebuild, so nothing remains to remove (issue #839, closed by the same Code Insights work as issue #873, see [Bitbucket setup](bitbucket-setup#code-insights-annotation-category-mapping)). To restore the older Bitbucket behavior of every finding in the comment body with no suppression, set `AI_BITBUCKET_CODE_INSIGHTS=false`.

To turn off GitHub reuse completely and always post a fresh review, set `AI_CANONICAL_REUSE=false`. See [Configuration](configuration#quiet-reruns-and-cross-run-finding-dedup).

**Concurrency note**: Two runs can start on the same PR close together (rapid pushes with no `concurrency:` group). Then one run can overwrite the write of the other run to the canonical review, in a narrow window. Just before **the body `PUT`**, the action checks the state of the canonical review again. If anything changed, the action posts a fresh review. This check covers only that write. The per-thread comment updates, the notification replies, and the call that dismisses the superseded review have no such check. The check makes the window smaller but does not remove it. If your repository gets frequent pushes, set a GitHub Actions `concurrency:` group keyed on the PR number. See `examples/workflows/pr-review.yml` for the example that ships with the action.

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, and so on), the review continues with the remaining agents and notes which agents it skipped. If all finding agents fail, the review stops.

**LLM retries**: The tool retries transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient network errors (connection refused, timeout). It uses exponential backoff with jitter. The `LLM_RETRY_COUNT` environment variable controls the retry count (default: 3).

**Parallel execution**: Agents run in a tiered fan-out by default. One shared concurrency limiter dispatches them, whatever the tier. Up to 4 LLM calls run at the same time (`concurrency`, controlled by `parallel` and `AI_PARALLEL`), alongside any triggered static analyzers. The concurrency limit applies to LLM calls only (for rate-limit planning). Static analyzers run at the same time but do not use LLM quota. If your provider's rate limits cannot sustain this throughput, set `parallel: false` to run in sequence (concurrency drops to 1).

**VCS API retries**: Calls to the GitHub, GitLab, and Bitbucket APIs (posting reviews and comments) retry up to 3 attempts. They retry on HTTP 429, 502, 503, and 504, on timeouts and network errors, and on 5xx responses that name a transient error. The wait between attempts doubles, with jitter.

**Truncation recovery**: If an LLM response is truncated (it hit the max tokens limit), the action tries to recover valid findings from the partial JSON. It does not discard the whole agent output.

## Token usage

By default (`token-usage-display: compact`), each posted review comment has one italic summary line. It does not have the full table:

> _Review cost: $0.1234 · 45,678 tokens · 8 agents · Sonnet 5 · [full breakdown](run-url)_

- **Review cost** is the estimated total for the run at public list rates. It has a `+` suffix when any agent used a model that is not in `config/model-pricing.json` (the true cost is at least this amount).
- **N agents** counts the finding agents that ran. The synthetic `judge-pass` row (see below) adds its tokens and cost to the total, but the tool does not count it as an agent.
- **[full breakdown]** links to the current CI run when the platform provides one (GitHub Actions, GitLab CI/CD job, or Bitbucket Pipelines, see `ci_run_url()` in `ai_pr_review/review/reporting.py`). If the related environment variables are empty, the tool leaves out the link and does not create a broken one.

To show the full `<details>` table inside the comment (the behavior before this line existed), set `token-usage-display: full`. To leave out all token-usage content from the comment, set `off`. The `token-usage-warn-usd` input (default `1.00`, `0` turns it off) adds a separate warning line when the estimated cost of a run crosses the threshold. The tool never combines that line with the table or the compact line. For both inputs, see [Configuration](configuration#token-usage-display).

Whatever the `token-usage-display` value, the full per-agent breakdown is always available in two other places:

- **The CI job log**: The tool writes it to stderr on every run, for every provider (GitHub, GitLab, Bitbucket).
- **The [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary)**: GitHub only.

The long-lived PR summary comment holds only the walkthrough from the first run. The tool does not rewrite it on later runs. Token-usage content is in the review body (GitHub) or the summary note (GitLab and Bitbucket), not in the walkthrough.

The layout of the full table depends on cache activity:

| Column | Description | When shown |
|--------|-------------|------------|
| Agent | Agent name | Always |
| Model | Human-readable model name (for example "Sonnet 4.6") | Always |
| Input | Input tokens consumed | Always |
| Output | Output tokens generated. Shown as `actual / cap` when a per-agent output cap is configured | Always |
| Cache Write | Tokens written to prompt cache | When any row has cache activity |
| Cache Read | Tokens read from prompt cache | When any row has cache activity |
| Total | Combined token count | Always |
| Est. Cost | Estimated cost at public list prices | Always |

When `LLM_PROMPT_CACHING` is active (default `auto` for Anthropic and Bedrock), the table grows to 8 columns. Cache Write and Cache Read show next to the standard columns.

The `judge-pass` row shows as a regular agent row (included in the Total) when the judge ran:

| Row | Description | When shown |
|-----|-------------|------------|
| `judge-pass` | Tokens consumed by the judge-pass LLM call. Included in Total | When `AI_JUDGE_PASS=true` (default) and the judge ran on a non-empty finding set |

Three supplementary rows can appear after the **Total** row. They are for information only and do not change the cost totals:

| Row | Description | When shown |
|-----|-------------|------------|
| Context enrichment | Token count of the `<symbol-context>` block prepended to agent prompts | When `AI_CONTEXT_ENRICHMENT=1` and the enrichment block was non-empty |
| Language profiles | Maximum profile tokens injected across all agents (every detected language's full profile goes to every eligible agent, issue #814) | When language profiles were injected and the count was non-zero |
| SARIF ingestion | Wall-clock elapsed time for parsing SARIF files (e.g. `0.34s`) | When `AI_SARIF_PATHS` is configured |

The tool calculates costs with public list prices. The costs do not include enterprise discounts, committed use agreements, or proxy markups.
