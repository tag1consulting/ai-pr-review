# AI PR Review

AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline review findings directly on the PR.

## Quickstart

Get AI reviews on your PRs in two steps:

**1. Add your LLM API key** as a repository secret named `ANTHROPIC_API_KEY` (or the equivalent for your [provider](#supported-llm-providers)).

**2. Create `.github/workflows/ai-review.yml`** with this minimal workflow:

```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: tag1consulting/ai-pr-review/container-action@main
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That's it — reviews start firing on the next PR. **Want slash commands?** (`/ai-pr-review rescan`, `review-full`, etc.) — see [Slash commands](#slash-commands) for the additional workflow file. See [Installation](#installation) for full-mode agents, provider configuration, and other options.

## Supported VCS providers

The same container image drives PR/MR reviews on GitHub, Bitbucket Cloud,
and GitLab. Select the provider via the `VCS_PROVIDER` env var (default: `github`).

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval | Standalone |
|----------|---------------|---------|--------|-------------|----------|------------|
| GitHub | `github` (default) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Bitbucket Cloud | `bitbucket` | ✅ | ❌ | ❌ | ❌ | ❌ |
| GitLab | `gitlab` | ✅ | ✅ | ✅ | ✅ | ✅ |

See [docs/bitbucket-setup.md](docs/bitbucket-setup.md) for Bitbucket Pipelines
setup and [docs/gitlab-setup.md](docs/gitlab-setup.md) for GitLab CI/CD setup
(token scopes, CI variables, starter pipeline, caveats). The remainder of
this README applies to the GitHub path.

## What it does

On every PR push, this action:

1. Computes the diff (full on first run, incremental on subsequent pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Runs deterministic checks on changed files: shellcheck, CVE lookups ([OSV.dev](https://osv.dev/)), semgrep SAST, trufflehog secret scanning, ruff (Python), golangci-lint (Go), hadolint (Dockerfiles), checkov (Terraform/K8s/IaC), phpcs (PHP/Drupal), eslint (JS/TS), phpstan (PHP static analysis), kube-linter (Kubernetes), and tflint (Terraform)
5. Posts a summary comment (first run only) and a review with inline findings
6. Auto-resolves stale bot threads and dismisses superseded reviews

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
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` / `claude-opus-4-7` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o` / `gpt-4o` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-4-6` / `global.anthropic.claude-opus-4-7` |

## Requirements

**The container action is the recommended way to run ai-pr-review.** It pulls a public image from GHCR — no additional authentication or toolchain setup required. All analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, eslint, phpstan, kube-linter, tflint) ship pre-installed at pinned versions.

If you prefer to run without Docker (e.g., on self-hosted runners without container support), the [direct action reference](docs/installation-direct-action.md) and [git submodule](docs/installation-submodule.md) methods work as standard GitHub Actions composite actions. These require:

- **Bash 4+**, **curl**, **jq**, **git**, **gh** — all pre-installed on standard GitHub-hosted runners
- **shellcheck** — installed automatically by the action if not already present
- Static analyzer binaries installed separately if desired (see [runtime dependencies](docs/installation-direct-action.md#runtime-dependencies))

Both methods require:

- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repos; see [installation notes](#installation) for exceptions)
- An API key for one of the [supported LLM providers](#supported-llm-providers)

## Installation

The container action is the recommended installation method — it ships all analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint) pre-installed at pinned, verified versions. No toolchain setup on your runner. See [Quickstart](#quickstart) for the minimal two-step setup.

### Full setup

The example workflow in [examples/workflows/pr-review.yml](examples/workflows/pr-review.yml) uses the container action:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  with:
    image-tag: 'latest'            # or pin to a release tag, e.g. '0.5.1'
    provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
    api-key: ${{ secrets.AI_REVIEW_API_KEY }}
    base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
    review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || 'quick' }}
    force-full-diff: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') && 'true' || 'false' }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

See [examples/README.md](examples/README.md) for a complete setup walkthrough including slash commands and provider configuration.

**Secrets and variables** — configure in the consuming repository's settings:

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `AI_REVIEW_API_KEY` | Secret | Yes | API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | Variable | No | Provider name (default: `anthropic`) |
| `AI_REVIEW_BASE_URL` | Variable | No | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Variable | No | Override the standard model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Variable | No | Override the premium model ID (full mode only) |

**Local development** — run reviews against any open PR without a CI runner:

```bash
# Dry run: prints findings to stdout, does not post to GitHub
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=$(gh pr view 42 --repo owner/repo --json headRefOid --jq .headRefOid) \
  -e AI_DRY_RUN=true \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

Remove `-e AI_DRY_RUN=true` to post findings back to the PR. Swap `AI_PROVIDER` and the corresponding key variable for other providers (`openai`/`OPENAI_API_KEY`, `google`/`GOOGLE_API_KEY`, `bedrock-proxy`/`BEDROCK_API_KEY`+`BEDROCK_API_URL`).

See [docs/local-development.md](docs/local-development.md) for the full reference including provider-specific examples, local clone mounting, git worktree support, and version pinning.

### Other installation methods

- **[Direct action reference](docs/installation-direct-action.md)** — uses the root composite action directly, without Docker. Installs shellcheck automatically; does not install semgrep, trufflehog, ruff, or golangci-lint.
- **[Git submodule](docs/installation-submodule.md)** — explicit, auditable version pinning; commits the exact action source into your repository. Uses a 3-job pattern to isolate the PAT used for submodule checkout.

## Slash commands

Once the comment-trigger workflow is merged to your default branch, users with write access can post commands on any PR:

| Command | Effect |
|---|---|
| `/ai-pr-review rescan` | Force full-diff re-review |
| `/ai-pr-review review-full` | Run all agents (full mode) |
| `/ai-pr-review skip` | Add `skip-ai-review` label |
| `/ai-pr-review help` | Post command list as reply |

Copy [examples/workflows/comment-triggers.yml](examples/workflows/comment-triggers.yml) to `.github/workflows/` in your repo. See [docs/slash-commands.md](docs/slash-commands.md) for details and the default-branch dispatch gotcha.

## Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `anthropic` | LLM provider |
| `api-key` | **Yes** | — | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `quick` | `quick` or `full` |
| `force-full-diff` | No | `false` | Bypass the SHA watermark; review the full PR diff for this run |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone` (GitHub issue) |
| `standalone-depth` | No | `''` | Commits to diff when base and head resolve to the same SHA |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `retry-count` | No | `3` | Retry attempts for transient LLM failures |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). Set to `false` to revert to sequential if you hit provider rate limits |
| `confidence-threshold` | No | `75` | Minimum finding confidence score (0–100); findings below this are dropped |
| `max-inline` | No | `25` | Maximum inline review comments per run; excess routed to the review body |
| `max-tokens-per-agent` | No | `8192` | Max output tokens per LLM agent call (clamped to 256–65536) |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab; ignored on Bitbucket). Set to `false` to disable. See [Code suggestions](#code-suggestions) |

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap — suitable for every push.

**Full mode**: Runs up to 8 agents — 6 always-on finding agents (code-reviewer, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general), plus silent-failure-hunter (conditional) and pr-summarizer on first run. Trigger full mode by:
- Adding the `ai-review-full` label to the PR
- Using `workflow_dispatch` with `review_mode: full`
- Setting the `review-mode` input to `full`
- Auto-detecting release PRs (see below)

### Auto-detecting release PRs

Full mode can be triggered automatically based on branch name, PR title, or other PR metadata by extending the `review-mode` expression in your workflow file. This keeps release PRs from getting only a quick review without requiring someone to remember to add a label.

The [example workflow](examples/workflows/pr-review.yml) demonstrates auto-detecting `release/*` branches:

```yaml
review-mode: >-
  ${{
    contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' ||
    startsWith(github.event.pull_request.head.ref, 'release/') && 'full' ||
    'quick'
  }}
```

Customize the `startsWith()` pattern for your repo's branch convention. Common patterns:

| Convention | Expression |
|---|---|
| Branch prefix `release/` | `startsWith(github.event.pull_request.head.ref, 'release/')` |
| Branch prefix `hotfix/` | `startsWith(github.event.pull_request.head.ref, 'hotfix/')` |
| PR title starts with "Release" | `startsWith(github.event.pull_request.title, 'Release ')` |
| Merges to `main` from `release/*` | `github.event.pull_request.base.ref == 'main' && startsWith(github.event.pull_request.head.ref, 'release/')` |
| Multiple patterns | Chain with `\|\|` — each clause evaluates to `'full'` or falls through |

Branch matching is case-sensitive — `release/v1.0` matches but `Release/v1.0` does not.

**Bitbucket Pipelines** — use a shell conditional instead of GitHub Actions expressions:

```bash
if [[ "$BITBUCKET_PR_SOURCE_BRANCH" == release/* ]]; then
  export AI_REVIEW_MODE=full
fi
```

## Code suggestions

Code suggestions are enabled by default. The review tool asks eligible LLM agents to emit concrete code fixes alongside their findings. Each fix is rendered as a ```` ```suggestion ```` block inside the inline review comment, which GitHub and GitLab display as an "Apply suggestion" button — the PR/MR author can accept the fix with one click.

To disable suggestions, set `enable-suggestions: false`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@v0.5.1
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    enable-suggestions: false
```

**Eligible agents** (those most likely to produce concrete line-level fixes): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`. Design-level agents (`architecture-reviewer`, `adversarial-general`) and static analyzers (shellcheck, semgrep, ruff, etc.) never emit suggestions.

**How it works.** Eligible agents have a short prompt addendum appended to their system prompt instructing them to include a `suggested_code` field (and optional `start_line` for multi-line replacements) only when the fix is concrete and complete. The post-review script constructs the ```` ```suggestion ```` fence itself — agents are not trusted to emit the markdown directly. Multi-line suggestions are validated against the diff: every line in the replacement range must appear on the new-file side of a diff hunk, or the suggestion is dropped while keeping the natural-language remediation.

**Caveats.** Suggestions increase output token usage. The feature works on both GitHub and GitLab (using GitLab's `suggestion` fence syntax) — Bitbucket reviews ignore it. Suggestions are validated defensively: `start_line` must be a positive integer ≤ `line` with no leading zeros, multi-line ranges are capped at 100 lines, and `suggested_code` containing triple backticks (which would break the suggestion fence) is rejected. When any validation fails, the suggestion is dropped with a WARNING logged to the Actions run and the finding still posts with its natural-language remediation. On incremental reviews (SHA watermark active), suggestions only render when the finding's line range is still in the current incremental diff — add the `ai-review-rescan` label to force a full re-review.

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

## Suppression system

Known false positives can be suppressed via `config/suppressions.json`. Each entry matches findings by file, line, code prefix, or regex pattern:

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

### Local suppressions

Consuming repos can add their own suppression rules without modifying the action. Create `.github/ai-pr-review/suppressions.json` in your repository using the same schema:

```json
[
  {
    "id": "my-repo-specific-rule",
    "reason": "Why this finding is not relevant to this repo",
    "match": {
      "pattern": "regex.*to.*match.*finding.*text"
    }
  }
]
```

Local rules are merged with the global suppression rules at runtime — no action input or configuration is required.

## Language profiles

The action auto-detects languages from file extensions and injects per-language context into agent prompts. Language profiles are markdown files in `language-profiles/`:

| Profile file | Covers |
|---|---|
| `go.md` | Go |
| `php.md` | PHP / Drupal |
| `python.md` | Python |
| `shell.md` | Shell / Bash |
| `typescript.md` | TypeScript / JavaScript |
| `ruby.md` | Ruby / Rails |
| `rust.md` | Rust |
| `java.md` | Java |
| `c++.md` | C and C++ |

To add a new language, create a `language-profiles/<language>.md` file. The filename (without extension) must match the lowercase language key returned by `detect_language()` in `review.sh` for the relevant file extensions. See CLAUDE.md for the full extension-to-language mapping.

## Dependency vulnerability check

When a PR modifies a supported dependency manifest, the action queries [OSV.dev](https://osv.dev/) for known vulnerabilities affecting the declared versions and surfaces them as findings alongside the LLM review.

| Manifest | Ecosystem |
|----------|-----------|
| `go.mod` | Go |
| `package.json` | npm |
| `requirements.txt` | PyPI |
| `composer.json` | Packagist |

Findings are mapped from CVSS score: ≥ 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, below 4.0 or unscored → Low. Critical and High findings trigger `REQUEST_CHANGES` on the PR review just like any other high-severity finding.

No configuration is required — the check runs automatically when a manifest file is in the diff. The OSV.dev API is unauthenticated and free. If the API is unreachable, the check emits a warning and continues — the review is never blocked by CVE-lookup failures.

To accept a specific CVE (e.g. library used only in a test fixture), add a suppression rule matching the CVE or GHSA ID:

```json
{
  "id": "accept-risk-CVE-2025-12345",
  "reason": "Library used only in test fixtures, not production",
  "match": {
    "pattern": "CVE-2025-12345|GHSA-xxxx-yyyy-zzzz"
  }
}
```

## Static analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path and fall back to sequential when `parallel: false`. If a binary is missing, the wrapper script emits a WARNING to stderr and returns `[]` — the review is never blocked.

The container action ships all analyzer binaries pre-installed. For the direct-action or submodule paths, install the binaries you need; see [docs/installation-direct-action.md](docs/installation-direct-action.md#runtime-dependencies).

| Analyzer | Language gate | Severity mapping | Confidence | Source tag |
|----------|--------------|-----------------|------------|------------|
| **shellcheck** | `.sh`, `.bash` | `error`→High, `warning`→Medium | 95 | `shellcheck` |
| **semgrep** | Any file | `ERROR`→High, `WARNING`→Medium, else→Low | 90 | `semgrep` |
| **trufflehog** | Any file | Verified secret→Critical, Unverified→High | 95 / 85 | `trufflehog` |
| **ruff** | `.py` files | `F`/`E` prefix→High, `W`/`C`→Medium, else→Low | 90 | `ruff` |
| **golangci-lint** | `.go` files | `errcheck`/`govet`/`staticcheck`→High, others→Medium | 90 | `golangci-lint` |
| **hadolint** | `Dockerfile*`, `*.dockerfile` | `error`→High, `warning`→Medium, else→Low | 90 | `hadolint` |
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | All→Medium (checkov has no per-check severity) | 80 | `checkov` |
| **phpcs** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | `ERROR`→High, `WARNING`→Medium; Drupal+DrupalPractice standard when available, else PSR12 | 90 | `phpcs` |
| **eslint** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | severity 2→High, severity 1→Medium; uses consumer's config — no-op if no `eslint.config.*` or `.eslintrc.*` found | 90 | `eslint` |
| **phpstan** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | All findings→High; runs at level `PHPSTAN_LEVEL` (default 3) unless consumer has `phpstan.neon`/`phpstan.neon.dist` | 85 | `phpstan` |
| **kube-linter** | `.yaml`, `.yml`, `.json` with `apiVersion:` + `kind:` headers | All findings→Medium (reliability-focused: missing probes, resource limits, etc.) | 85 | `kube-linter` |
| **tflint** | `.tf`, `.tfvars` | `error`→High, `warning`→Medium, `notice`→Low; runs per Terraform module directory | 90 | `tflint` |

CVE check queries [OSV.dev](https://osv.dev/) against `go.mod`, `package.json`, `requirements*.txt`, and `composer.json`. See [Dependency vulnerability check](#dependency-vulnerability-check) for details.

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
├── post-review-bitbucket.sh # Bitbucket Cloud API posting: summary comment
├── post-review-gitlab.sh   # GitLab API posting: summary, inline discussions, approval
├── analyzers/              # Static analyzer wrapper scripts
│   ├── run-shellcheck.sh   # Shellcheck wrapper for shell script findings
│   ├── run-cve-check.sh    # OSV.dev vulnerability lookup for dependency manifests
│   ├── run-semgrep.sh      # Semgrep SAST wrapper (optional binary)
│   ├── run-trufflehog.sh   # Trufflehog secret scanning wrapper (optional binary)
│   ├── run-ruff.sh         # Ruff Python linter wrapper (optional binary)
│   ├── run-golangci-lint.sh # golangci-lint Go linter wrapper (optional binary)
│   ├── run-hadolint.sh     # Hadolint Dockerfile linter wrapper (optional binary)
│   ├── run-checkov.sh      # Checkov IaC scanner wrapper (optional binary)
│   ├── run-phpcs.sh        # PHP_CodeSniffer wrapper, Drupal+DrupalPractice standard
│   ├── run-eslint.sh       # ESLint JS/TS wrapper — uses consumer config, no-op if absent
│   ├── run-phpstan.sh      # PHPStan static analysis wrapper (optional binary)
│   ├── run-kube-linter.sh  # kube-linter Kubernetes manifest wrapper (optional binary)
│   └── run-tflint.sh       # tflint Terraform linter wrapper (optional binary)
├── config/                 # Configuration and data files
│   ├── model-pricing.json  # Per-model token pricing for cost estimation
│   └── suppressions.json   # Declarative false-positive suppression rules
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
│   ├── python.md
│   ├── typescript.md
│   ├── php.md
│   ├── shell.md
│   ├── ruby.md
│   ├── rust.md
│   ├── java.md
│   └── c++.md
├── tests/                  # bats-core unit tests
│   ├── review_functions.bats
│   ├── extract_findings.bats
│   ├── is_test_file.bats
│   ├── configurable_inputs.bats
│   ├── dedup_filter.bats
│   ├── apply_suppressions.bats
│   ├── call_agent.bats
│   ├── llm_call_functions.bats
│   ├── post_review_functions.bats
│   ├── run_shellcheck.bats
│   ├── run_cve_check.bats
│   ├── run_semgrep.bats
│   ├── run_trufflehog.bats
│   ├── run_ruff.bats
│   ├── run_golangci_lint.bats
│   ├── run_hadolint.bats
│   ├── run_checkov.bats
│   ├── run_phpcs.bats
│   ├── run_eslint.bats
│   ├── run_phpstan.bats
│   ├── run_kube_linter.bats
│   ├── run_tflint.bats
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
5. The **provider-specific post-review script** resolves stale threads, posts the summary and findings, advances the SHA watermark

### Dependencies

The action requires `bash`, `curl`, `jq`, `git`, and `gh` — all pre-installed on standard GitHub-hosted runners. `shellcheck` is installed automatically if not already present.

The **container action** (recommended) ships all static analyzer binaries pre-installed at pinned versions — no runner setup needed. The **direct action reference** and **git submodule** paths do not install analyzer binaries; see [docs/installation-direct-action.md](docs/installation-direct-action.md#runtime-dependencies) for the optional install-in-workflow snippet.

## License

MIT
