# AI PR Review

AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline review findings directly on the PR.

## Requirements

The action runs on `ubuntu-latest` GitHub Actions runners and requires:

- **Bash 4+**, **curl**, **jq**, **git**, **gh** — all pre-installed on standard GitHub-hosted runners
- **shellcheck** — installed automatically by the action if not already present
- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repos; see [installation notes](#installation) for exceptions)
- An API key for one of the [supported LLM providers](#supported-llm-providers)

No additional runner setup or Docker image is required.

## What it does

On every PR push, this action:

1. Computes the diff (full on first run, incremental on subsequent pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Posts a summary comment (first run only) and a review with inline findings
5. Auto-resolves stale bot threads and dismisses superseded reviews

### Review agents

**Quick mode** (default) runs 1-2 finding agents plus a summary agent on first run:

| Agent | Purpose |
|-------|---------|
| **pr-summarizer** | Generates a walkthrough summary (first run only) |
| **code-reviewer** | Finds bugs, logic errors, and code quality issues |
| **silent-failure-hunter** | Detects swallowed errors and unsafe fallbacks (runs when error-handling patterns are detected) |

**Full mode** adds 5 more agents:

| Agent | Purpose |
|-------|---------|
| **architecture-reviewer** | Evaluates design patterns, coupling, and scalability |
| **security-reviewer** | Checks for injection, auth, crypto, and supply chain issues |
| **blind-hunter** | Context-free review (zero project knowledge, catches familiarity blindness) |
| **edge-case-hunter** | Traces every branching path for unhandled gaps |
| **adversarial-general** | Cynical adversarial review |

### Severity icons

Findings use shape-distinct icons for accessibility:

| Icon | Severity | Review action |
|------|----------|---------------|
| ❌ | Critical | REQUEST_CHANGES |
| 🚨 | High | REQUEST_CHANGES |
| 🔶 | Medium | APPROVE (informational) |
| 💬 | Low | APPROVE (informational) |

## Supported LLM providers

| Provider | `provider` value | Required secret | Default models (standard / premium) |
|----------|-----------------|-----------------|--------------------------------------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` / `claude-opus-4-6` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o` / `gpt-4o` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-4-6` / `global.anthropic.claude-opus-4-6-v1` |

## Installation

### Direct action reference (recommended)

This is the simplest approach. No submodule or extra checkout configuration needed.

**Prerequisites:** In this repo's settings, go to **Settings → Actions → General → Access** and set it to **"Accessible from repositories in the 'tag1consulting' organization"**. This allows other repos in the org to use it as an action.

#### 1. Create the workflow

Create `.github/workflows/ai-review.yml` in your repository:

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, labeled]

permissions:
  contents: read
  pull-requests: write
  issues: write           # needed for standalone review mode

jobs:
  review:
    concurrency:
      group: ai-review-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    if: >-
      github.event.pull_request.draft == false &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0

      - uses: tag1consulting/ai-pr-review@main
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

Pin to a specific version by using a tag or commit SHA instead of `@main` (e.g., `@v1.0` or `@d613707`).

#### 2. Configure secrets and variables

In the **consuming** repository's settings:

**Secrets:**
- `AI_REVIEW_API_KEY` — API key for your chosen LLM provider

**Variables** (optional):
- `AI_REVIEW_PROVIDER` — Provider name (default: `anthropic`)
- `AI_REVIEW_BASE_URL` — Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`)
- `AI_REVIEW_MODEL_STANDARD` — Override the standard model ID
- `AI_REVIEW_MODEL_PREMIUM` — Override the premium model ID (full mode only)

### Alternative: git submodule

If you prefer explicit version pinning via a submodule (useful for auditing exactly which version of the action is used):

```bash
git submodule add git@github.com:tag1consulting/ai-pr-review.git ai-pr-review
git commit -m "Add ai-pr-review submodule"
```

Then in your workflow, use `submodules: true` on checkout and reference the local path:

```yaml
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
          submodules: true
          # GITHUB_TOKEN cannot check out a submodule in a different private repo.
          # A PAT with repo scope (or fine-grained token with read access to
          # tag1consulting/ai-pr-review) is required:
          token: ${{ secrets.AI_PR_REVIEW_TOKEN }}

      - uses: ./ai-pr-review
        with:
          # ... same inputs as above
```

To update the submodule pin:

```bash
cd ai-pr-review
git fetch --all
git checkout v1.0
cd ..
git add ai-pr-review
git commit -m "Bump ai-pr-review submodule to v1.0"
```

## Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `anthropic` | LLM provider |
| `api-key` | **Yes** | — | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `quick` | `quick` or `full` |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone` (GitHub issue) |
| `standalone-depth` | No | `''` | Commits to diff when base and head resolve to the same SHA |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `retry-count` | No | `3` | Retry attempts for transient LLM failures |

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap — suitable for every push.

**Full mode**: Runs up to 8 agents — 6 always-on finding agents (code-reviewer, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general), plus silent-failure-hunter (conditional) and pr-summarizer on first run. Trigger full mode by:
- Adding the `ai-review-full` label to the PR
- Using `workflow_dispatch` with `review_mode: full`
- Setting the `review-mode` input to `full`

## Incremental reviews

After the first full-PR review, subsequent pushes trigger an incremental review that only analyzes the new commits. The SHA watermark is stored in the summary comment and advanced after each review run.

If the watermark cannot be found (e.g., the summary comment was deleted), the action falls back to a full PR diff.

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, etc.), the review continues with the remaining agents and notes which agents were skipped. If all finding agents fail, the review is aborted.

**LLM retries**: Transient API failures (HTTP 429, 500, 502, 503) and transient curl errors (connection refused, timeout) are retried with exponential backoff and jitter. Controlled by the `retry-count` input (default: 3).

**GitHub API retries**: Critical GitHub API calls (posting reviews, comments) retry on 502, 503, 429, and ETIMEDOUT with fixed backoff.

**Truncation recovery**: When an LLM response is truncated (hit max tokens), the action attempts to salvage valid findings from the partial JSON rather than discarding the entire agent output.

## Suppression system

Known false positives can be suppressed via `suppressions.json`. Each entry matches findings by file, line, code prefix, or regex pattern:

```json
[
  {
    "id": "descriptive-id",
    "reason": "Why this is a false positive",
    "match": {
      "file": "specific-file.sh",
      "pattern": "regex.*to.*match.*finding.*text"
    }
  }
]
```

Match fields (all optional, combined with AND logic):
- `file` — Substring match on the finding's file path
- `line` — Exact line number match
- `code` — Finding text starts with this prefix
- `pattern` — Regex matched against the finding text

## Language profiles

The action auto-detects languages from file extensions and injects per-language context into agent prompts. Language profiles are markdown files in `language-profiles/`:

- `go.md` — Go-specific review context
- `shell.md` — Shell/Bash-specific review context

To add a new language, create a `language-profiles/<language>.md` file. The filename (without extension) should match the language key detected from file extensions.

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

Costs are calculated using public list prices as of April 2026 and do not reflect enterprise discounts, committed use agreements, or proxy markups. The table is also written to the [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary) for easy access from the Actions run page.

## Architecture

```
ai-pr-review/
├── action.yml              # GitHub Actions composite action definition
├── review.sh               # Main orchestrator: diff, manifest, agent calls, assembly
├── llm-call.sh             # Multi-provider LLM API wrapper (curl-based)
├── post-review.sh          # GitHub API posting: summary, review, thread management
├── run-shellcheck.sh       # Shellcheck wrapper for shell script findings
├── model-pricing.json      # Per-model token pricing for cost estimation
├── suppressions.json       # Declarative false-positive suppression rules
├── prompts/                # System prompts for each review agent
│   ├── pr-summarizer.md
│   ├── code-reviewer.md
│   ├── silent-failure-hunter.md
│   ├── architecture-reviewer.md
│   ├── security-reviewer.md
│   ├── blind-hunter.md
│   ├── edge-case-hunter.md
│   └── adversarial-general.md
├── language-profiles/      # Per-language review context
│   ├── go.md
│   └── shell.md
├── tests/                  # bats-core unit tests
│   ├── review_functions.bats
│   ├── extract_findings.bats
│   ├── llm_call_functions.bats
│   ├── post_review_functions.bats
│   ├── test_helper.bash
│   └── fixtures/
└── .github/workflows/
    ├── ai-review.yml       # Self-test: runs the action on its own PRs
    └── lint.yml            # Shellcheck + bats test suite
```

### Data flow

1. **review.sh** computes the diff, builds a file manifest, detects languages
2. For each agent, **review.sh** assembles a context message and calls **llm-call.sh**
3. **llm-call.sh** sends the prompt to the configured LLM provider via curl
4. **review.sh** extracts JSON findings from agent responses, deduplicates, applies suppressions
5. **post-review.sh** resolves stale threads, posts the summary and findings, advances the SHA watermark

### Dependencies

The action runs on `ubuntu-latest` and requires only standard tools:
- `bash`, `curl`, `jq`, `git`, `gh` (all pre-installed on GitHub Actions runners)
- `shellcheck` (installed automatically by the action if not present)

## License

MIT
