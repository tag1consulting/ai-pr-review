# AI PR Review

AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline review findings directly on the PR.

## Requirements

The action runs on `ubuntu-latest` GitHub Actions runners and requires:

- **Bash 4+**, **curl**, **jq**, **git**, **gh** — all pre-installed on standard GitHub-hosted runners
- **shellcheck** — installed automatically by the action if not already present
- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repos; see [installation notes](#installation) for exceptions)
- An API key for one of the [supported LLM providers](#supported-llm-providers)

No additional runner setup or Docker image is required.

## Supported VCS providers

The same container image drives PR reviews on both GitHub and Bitbucket Cloud.
Select the provider via the `VCS_PROVIDER` env var (default: `github`).

| Provider | `VCS_PROVIDER` | Summary comment | Inline findings | Standalone (issue) mode |
|----------|---------------|-----------------|-----------------|------------------------|
| GitHub | `github` (default) | ✅ | ✅ | ✅ |
| Bitbucket Cloud | `bitbucket` | ✅ (findings rendered inside) | ❌ (v0.2.0) | ❌ (no Issues product) |

See [docs/bitbucket-setup.md](docs/bitbucket-setup.md) for Bitbucket Pipelines
setup (token scopes, repo variables, starter pipeline, caveats). The
remainder of this README applies to the GitHub path.

## What it does

On every PR push, this action:

1. Computes the diff (full on first run, incremental on subsequent pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Runs deterministic checks on changed files: shellcheck, CVE lookups ([OSV.dev](https://osv.dev/)), semgrep SAST, trufflehog secret scanning, ruff (Python), golangci-lint (Go), hadolint (Dockerfiles), checkov (Terraform/K8s/IaC), phpcs (PHP/Drupal), and eslint (JS/TS)
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

## Installation

### Container action (recommended)

The container action pulls a pre-built image with all analyzer binaries pre-installed at pinned, verified versions. No toolchain setup on your runner.

See **[Running in a container](#running-in-a-container)** below for prerequisites (GHCR token) and the example workflows in `examples/workflows/`.

### Direct action reference (legacy / opt-in)

Use this if you cannot or do not want to pull a private container image. Installs shellcheck on the runner; does not install semgrep, trufflehog, ruff, or golangci-lint.

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

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    # head.repo.full_name check is defense-in-depth against fork PRs, which
    # cannot access secrets anyway but should not trigger review jobs that
    # hold pull-requests: write.
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.event.pull_request.draft == false &&
      github.actor != 'dependabot[bot]' &&
      github.actor != 'renovate[bot]' &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review') &&
      (github.event.action != 'labeled' ||
       github.event.label.name == 'ai-review-full' ||
       github.event.label.name == 'ai-review-rescan')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: tag1consulting/ai-pr-review@main
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || 'quick' }}
          force-full-diff: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') && 'true' || 'false' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}

  # Always attempt to remove the ai-review-rescan label after the review,
  # even if the review job was cancelled by the concurrency rule on a new push.
  # Exclude skip-ai-review PRs so we don't strip the rescan label from a PR
  # that intentionally bypasses the review.
  cleanup-rescan-label:
    needs: review
    if: >-
      always() &&
      github.event.pull_request.head.repo.full_name == github.repository &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
    steps:
      - name: Remove ai-review-rescan label
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh api \
            --method DELETE \
            repos/${{ github.repository }}/issues/${{ github.event.pull_request.number }}/labels/ai-review-rescan \
            || true
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
mkdir -p .github/actions
git submodule add git@github.com:tag1consulting/ai-pr-review.git .github/actions/ai-pr-review
git commit -m "Add ai-pr-review submodule"
```

Then create `.github/workflows/ai-review.yml` in your repository. The submodule approach uses a **3-job pattern** (`prepare` → `review` → `cleanup-rescan-label`) that isolates the PAT used for submodule checkout from the job that executes the composite action. This prevents the PAT from landing in the review job's `.git/config` where the action's shell scripts could read it.

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, labeled]

# Fork PRs cannot access secrets anyway; per-job guards below prevent
# untrusted fork branches from triggering jobs with write permissions.
permissions:
  contents: read
  pull-requests: write
  # issues: write is declared only on cleanup-rescan-label (least-privilege)

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  # Job 1: validate config and check out the repo (including the private
  # submodule) using the PAT. Credentials are scrubbed from .git/config
  # before uploading the workspace artifact so the PAT does not travel
  # to the review job.
  prepare:
    runs-on: ubuntu-latest
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.event.pull_request.draft == false &&
      github.actor != 'dependabot[bot]' &&
      github.actor != 'renovate[bot]' &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review') &&
      (github.event.action != 'labeled' ||
       github.event.label.name == 'ai-review-full' ||
       github.event.label.name == 'ai-review-rescan')
    steps:
      # Fail fast with a clear error when required config is missing, rather
      # than a cryptic git auth or API failure deeper in the pipeline.
      - name: Validate required configuration
        env:
          AI_REVIEW_API_KEY: ${{ secrets.AI_REVIEW_API_KEY }}
          AI_PR_REVIEW_TOKEN: ${{ secrets.AI_PR_REVIEW_TOKEN }}
        run: |
          missing=()
          [ -z "$AI_REVIEW_API_KEY" ] && missing+=("secret AI_REVIEW_API_KEY")
          [ -z "$AI_PR_REVIEW_TOKEN" ] && missing+=("secret AI_PR_REVIEW_TOKEN")
          if [ ${#missing[@]} -gt 0 ]; then
            echo "ERROR: Missing required configuration: ${missing[*]}"
            echo "See the README for setup instructions."
            exit 1
          fi

      # AI_PR_REVIEW_TOKEN needs repo scope (or fine-grained read access to
      # tag1consulting/ai-pr-review) to clone the private submodule.
      # GITHUB_TOKEN cannot cross-repo clone private submodules.
      - name: Checkout with submodules
        uses: actions/checkout@v4
        with:
          submodules: true
          token: ${{ secrets.AI_PR_REVIEW_TOKEN }}
          fetch-depth: 0

      # actions/checkout writes the PAT into .git/config as a persistent
      # credential. Scrubbing it before upload prevents the PAT from
      # travelling to the review job via the artifact.
      - name: Scrub git credentials before artifact upload
        run: git config --unset-all http.https://github.com/.extraheader || true

      - name: Upload workspace
        uses: actions/upload-artifact@v4
        with:
          name: workspace
          path: .
          include-hidden-files: true
          retention-days: 1

  # Job 2: run the composite action against the downloaded workspace.
  # This job does NOT run actions/checkout with the PAT, so the repo-scoped
  # PAT is never in its git config or accessible to the action.
  review:
    needs: prepare
    runs-on: ubuntu-latest
    if: github.event.pull_request.head.repo.full_name == github.repository
    steps:
      - name: Download workspace
        uses: actions/download-artifact@v4
        with:
          name: workspace
          path: .

      # actions/upload-artifact strips file permissions. Restore execute
      # bits on the composite action's shell scripts only — NOT on
      # .git/hooks/*, which would allow arbitrary code execution via a
      # malicious hook committed to the PR branch.
      - name: Restore executable bits
        run: find .github/actions/ai-pr-review -name "*.sh" -exec chmod +x {} +

      - uses: ./.github/actions/ai-pr-review
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || 'quick' }}
          force-full-diff: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') && 'true' || 'false' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}

  # Job 3: always attempt to remove the ai-review-rescan label.
  # needs: [prepare, review] with if: always() ensures this runs even when
  # the review job is cancelled (e.g. by the concurrency rule on a new push)
  # and when prepare is skipped (e.g. fork PRs). The gh api DELETE call
  # returns 404 when the label is absent; || true swallows it.
  cleanup-rescan-label:
    needs: [prepare, review]
    if: >-
      always() &&
      github.event.pull_request.head.repo.full_name == github.repository &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
    steps:
      - name: Remove ai-review-rescan label
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh api \
            --method DELETE \
            repos/${{ github.repository }}/issues/${{ github.event.pull_request.number }}/labels/ai-review-rescan \
            || true
```

> **Why 3 jobs for the submodule pattern?** `actions/checkout` with a PAT writes the token into `.git/config` as a persistent credential readable by any subsequent step in the same job. Isolating checkout into its own job (and scrubbing credentials before the workspace is passed to the review job as an artifact) keeps the PAT out of the job that executes third-party shell scripts.

To update the submodule pin:

```bash
cd .github/actions/ai-pr-review
git fetch --all
git checkout v1.0
cd ../../..
git add .github/actions/ai-pr-review
git commit -m "Bump ai-pr-review submodule to v1.0"
```

## Running in a container

The container image ships all analyzer binaries pre-installed at pinned versions, so consumers don't need to install shellcheck, semgrep, trufflehog, ruff, or golangci-lint on their runners.

### Registry authentication

The image is hosted privately at `ghcr.io/tag1consulting/ai-pr-review`. Every consumer needs a GitHub Personal Access Token with `read:packages` scope stored as a repository secret named `GHCR_TOKEN`.

Create the token at **Settings → Developer settings → Personal access tokens (classic)**, grant `read:packages`, then add it under **Settings → Secrets and variables → Actions** in your repo.

### Container action

Use `container-action` in place of the root action:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  with:
    image-tag: 'latest'            # or pin to '0.1.0'
    registry-token: ${{ secrets.GHCR_TOKEN }}
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ github.token }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
```

Ready-to-use workflow files are in [examples/workflows/](examples/workflows/). See [examples/README.md](examples/README.md) for setup instructions.

### Local development

Run reviews locally against any open PR — no CI runner needed.

```bash
# One-time: authenticate to GHCR (PAT with read:packages scope)
docker login ghcr.io -u YOUR_GITHUB_USERNAME -p YOUR_GHCR_PAT

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

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap — suitable for every push.

**Full mode**: Runs up to 8 agents — 6 always-on finding agents (code-reviewer, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general), plus silent-failure-hunter (conditional) and pr-summarizer on first run. Trigger full mode by:
- Adding the `ai-review-full` label to the PR
- Using `workflow_dispatch` with `review_mode: full`
- Setting the `review-mode` input to `full`

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
├── run-shellcheck.sh       # Shellcheck wrapper for shell script findings
├── run-cve-check.sh        # OSV.dev vulnerability lookup for dependency manifests
├── run-semgrep.sh          # Semgrep SAST wrapper (optional binary)
├── run-trufflehog.sh       # Trufflehog secret scanning wrapper (optional binary)
├── run-ruff.sh             # Ruff Python linter wrapper (optional binary)
├── run-golangci-lint.sh    # golangci-lint Go linter wrapper (optional binary)
├── run-hadolint.sh         # Hadolint Dockerfile linter wrapper (optional binary)
├── run-checkov.sh          # Checkov IaC scanner wrapper (optional binary)
├── run-phpcs.sh            # PHP_CodeSniffer wrapper, Drupal+DrupalPractice standard (optional binary)
├── run-eslint.sh           # ESLint JS/TS wrapper — uses consumer config, no-op if absent
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

**Optional static analyzer binaries** — the action degrades gracefully if these are absent (emits a WARNING and continues):
- `semgrep` — SAST analysis on any changed file
- `trufflehog` — secret scanning on any changed file
- `ruff` — Python linting (`.py` files only)
- `golangci-lint` — Go linting (`.go` files only); must be run from the Go module root

Install them in your workflow before invoking the action if you want their findings. Example:

```yaml
- name: Install static analyzers
  run: |
    pip install semgrep
    curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin
    pip install ruff
    curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh | sh -s -- -b /usr/local/bin
```

## License

MIT
