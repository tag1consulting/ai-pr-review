# AI PR Review

AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline review findings directly on the PR.

## What it does

On every PR push, this action:

1. Computes the diff (full on first run, incremental on subsequent pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Posts a summary comment (first run only) and a review with inline findings
5. Auto-resolves stale bot threads and dismisses superseded reviews

### Review agents

**Quick mode** (default) runs 2-3 agents:

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
| 🔶 | Medium | COMMENT |
| 💬 | Low | COMMENT |

## Supported LLM providers

| Provider | `provider` value | Required secret | Notes |
|----------|-----------------|-----------------|-------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | Claude Sonnet/Opus |
| OpenAI | `openai` | `OPENAI_API_KEY` | GPT-4o |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Any OpenAI-compatible endpoint |
| Google | `google` | `GOOGLE_API_KEY` | Gemini 2.5 Flash/Pro |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | Tag1 OpenWebUI Bedrock proxy (default) |

## Usage

### As a GitHub Actions composite action (submodule)

#### 1. Add this repo as a git submodule

```bash
git submodule add git@github.com:tag1consulting/ai-pr-review.git ai-pr-review
git commit -m "Add ai-pr-review submodule"
```

#### 2. Create the workflow

Create `.github/workflows/ai-review.yml`:

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review]

permissions:
  contents: read
  pull-requests: write

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
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          submodules: true
          # Use a PAT if the submodule is in a private repo:
          # token: ${{ secrets.GH_PAT }}

      - uses: ./ai-pr-review
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'bedrock-proxy' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

#### 3. Configure secrets and variables

In your repository settings:

**Secrets:**
- `AI_REVIEW_API_KEY` — API key for your chosen LLM provider

**Variables** (optional):
- `AI_REVIEW_PROVIDER` — Provider name (default: `bedrock-proxy`)
- `AI_REVIEW_BASE_URL` — Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`)
- `AI_REVIEW_MODEL_STANDARD` — Override the standard model ID
- `AI_REVIEW_MODEL_PREMIUM` — Override the premium model ID (full mode only)

### Updating the submodule

To pin the submodule to a new version:

```bash
cd ai-pr-review
git fetch --all
git checkout v1.0  # or a specific commit
cd ..
git add ai-pr-review
git commit -m "Bump ai-pr-review submodule to v1.0"
```

## Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `bedrock-proxy` | LLM provider |
| `api-key` | **Yes** | — | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `quick` | `quick` or `full` |
| `pr-number` | **Yes** | — | PR number |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap — suitable for every push.

**Full mode**: Runs all 8 agents including architecture, security, and adversarial review. Trigger full mode by:
- Adding the `ai-review-full` label to the PR
- Using `workflow_dispatch` with `review_mode: full`
- Setting the `review-mode` input to `full`

## Incremental reviews

After the first full-PR review, subsequent pushes trigger an incremental review that only analyzes the new commits. The SHA watermark is stored in the summary comment and advanced after each review run.

If the watermark cannot be found (e.g., the summary comment was deleted), the action falls back to a full PR diff.

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
- `file` — Exact filename match
- `line` — Exact line number match
- `code` — Finding text starts with this prefix
- `pattern` — Regex matched against the finding text

## Language profiles

The action auto-detects languages from file extensions and injects per-language context into agent prompts. Language profiles are markdown files in `language-profiles/`:

- `go.md` — Go-specific review context
- `shell.md` — Shell/Bash-specific review context

To add a new language, create a `language-profiles/<language>.md` file. The filename (without extension) should match the language key detected from file extensions.

## Architecture

```
ai-pr-review/
├── action.yml              # GitHub Actions composite action definition
├── review.sh               # Main orchestrator: diff, manifest, agent calls, assembly
├── llm-call.sh             # Multi-provider LLM API wrapper (curl-based)
├── post-review.sh          # GitHub API posting: summary, review, thread management
├── run-shellcheck.sh       # Shellcheck wrapper for shell script findings
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
└── language-profiles/      # Per-language review context
    ├── go.md
    └── shell.md
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
