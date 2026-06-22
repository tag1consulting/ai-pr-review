# AI PR Review

AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline review findings directly on the PR.

> **[Full documentation](https://tag1consulting.github.io/ai-pr-review)** | [Getting started](https://tag1consulting.github.io/ai-pr-review/getting-started) | [Configuration](https://tag1consulting.github.io/ai-pr-review/configuration) | [Contributing](CONTRIBUTING.md)

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
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: tag1consulting/ai-pr-review/container-action@main
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That's it — reviews start firing on the next PR.

**Going further:**
- [Slash commands](#slash-commands) — `/ai-pr-review rescan`, `review-full`, `dismiss`, and learning-loop commands (`false-positive`, `wont-fix`, `feedback`) — now built into the unified `pr-review.yml` template, no separate file needed
- [Opt-in capabilities](#opt-in-capabilities) — tree-sitter symbol-context enrichment (default on in the container image), SARIF 2.1.0 ingestion (CodeQL/Semgrep/Trivy), and the learning loop.
- [Installation](#installation) — full-mode agents, provider configuration, [`examples/workflows/pr-review.yml`](examples/workflows/pr-review.yml) for the complete repo-variable pattern used by internal consumers

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

Use the `agents` and `exclude-agents` inputs to control which agents run. See [Action inputs](#action-inputs).

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
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` / `claude-opus-4-8` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-5.4-mini` / `gpt-5.4` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-4-6` / `global.anthropic.claude-opus-4-7` |

### Provider-specific notes

**Anthropic** — The default and most-tested provider. Uses explicit prompt caching (`cache_control: ephemeral`) with a shared-cache layout that gives agents in the same cohort a single shared cache entry. Typical cold-run savings of ~47%, hot-run ~61%. Premium tier uses Opus for deeper reasoning on Tier 2 agents (architecture, security, edge-case).

**OpenAI** — Fully supported. Default models are `gpt-5.4-mini` (standard, Tier 1) and `gpt-5.4` (premium, Tier 2). Uses `max_completion_tokens` (the modern field; `max_tokens` is kept for `openai-compatible`). Automatic prefix caching (50% discount on cached input tokens for prompts ≥1024 tokens) is maximized via a shared-cache request layout that puts the shared review context first in the system message, allowing agents in the same cohort to share a common prefix. Cache hits are measured and reported in the token usage table. OpenAI reasoning models (`o3`, `o4-mini`) and reasoning-capable models (`gpt-5`, `gpt-5.5`) are supported — temperature is automatically omitted for these models. In benchmarks on a 162-line diff, OpenAI (gpt-5.4-mini + gpt-5.4) was ~79% cheaper than Anthropic (Sonnet + Opus) with comparable finding quality (all critical/high issues caught, zero false positives). Override with `model-standard` / `model-premium` inputs — `gpt-4o`, `gpt-4.1`, and `gpt-5.5` are all supported.

**OpenAI-compatible** — For third-party endpoints (Azure OpenAI, Groq, Together, local models). Uses the legacy `max_tokens` field for broader compatibility. Requires `base-url` and explicit `model-standard`/`model-premium` inputs.

**Google Gemini** — Fully supported. Uses `gemini-2.5-flash` (standard) and `gemini-2.5-pro` (premium). Gemini 2.5 models produce "thinking" tokens billed at the output rate — these are extracted, added to the output count for accurate cost estimation, and logged as `THINKING: N tokens` on stderr. On a 162-line benchmark, Gemini (Flash + Pro) cost $0.28 with 22 findings — 62% cheaper than Anthropic. The high output count is dominated by thinking tokens (79% of output). Implicit caching (`cachedContentTokenCount`) is extracted when present. `gemini-2.5-flash-lite` is also supported as a cheaper standard alternative ($0.10/$0.40 per MTok, no thinking tokens).

## Requirements

**The container action is the recommended way to run ai-pr-review.** It pulls a public multi-arch image from GHCR (linux/amd64 and linux/arm64) — no additional authentication or toolchain setup required. Most analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint) ship pre-installed at pinned versions. ESLint is not bundled (it runs from the consumer's `node_modules` / `npx` via the project's own config); the review proceeds without ESLint findings if no JS toolchain is present.

If you prefer to run without Docker (e.g., on self-hosted runners without container support), the [direct action reference](docs/installation-direct-action.md) and [git submodule](docs/installation-submodule.md) methods work as standard GitHub Actions composite actions. These require:

- **Bash 4+**, **curl**, **jq**, **git**, **gh** — all pre-installed on standard GitHub-hosted runners
- **shellcheck** — installed automatically by the action if not already present
- Other static-analyzer binaries installed separately if desired (see [runtime dependencies](docs/installation-direct-action.md#runtime-dependencies))

Both methods require:

- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repos; see [installation notes](#installation) for exceptions)
- An API key for one of the [supported LLM providers](#supported-llm-providers)

## Installation

The container action is the recommended installation method — it ships all analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint) pre-installed at pinned, verified versions. The image supports linux/amd64 and linux/arm64 natively (Apple Silicon, Graviton runners). No toolchain setup on your runner. See [Quickstart](#quickstart) for the minimal two-step setup.

### Full setup

The example workflow in [examples/workflows/pr-review.yml](examples/workflows/pr-review.yml) uses the container action:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  env:
    FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
  with:
    image-tag: ${{ vars.AI_REVIEW_IMAGE_TAG || 'latest' }}  # or pin to a release tag, e.g. '0.8.0'
    provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
    api-key: ${{ secrets.AI_REVIEW_API_KEY }}
    base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
    review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || 'quick' }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

See [examples/README.md](examples/README.md) for the complete setup walkthrough including [`examples/workflows/pr-review.yml`](examples/workflows/pr-review.yml) — the canonical template every internal Tag1 repo uses, with every input wired to a `vars.AI_REVIEW_*` repo variable with a safe fallback default.

**Secrets and variables** — configure in the consuming repository's settings. All variables are optional; the secret is required.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `AI_REVIEW_API_KEY` | Secret | Yes | API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | Variable | No | Provider name (default: `anthropic`) |
| `AI_REVIEW_BASE_URL` | Variable | No | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Variable | No | Override the standard model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Variable | No | Override the premium model ID (full mode only) |
| `AI_REVIEW_IMAGE_TAG` | Variable | No | Container image tag (default `latest`; set to `dev` to dogfood pre-release builds or pin to a release like `0.12.2` — image tags published by `publish-image.yml` strip the `v` prefix) |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | Variable | No | `false` to disable stripping of base-branch merge commits from the diff before review (default `true`) |
| `AI_REVIEW_CONTEXT_ENRICHMENT` | Variable | No | `true` to enable tree-sitter symbol-context injection (default `false`) |
| `AI_REVIEW_SARIF_PATHS` | Variable | No | Comma-separated SARIF 2.1.0 file paths to merge as findings (default `''`) |
| `AI_REVIEW_FEEDBACK_LOOP` | Variable | No | `true` to enable the learning loop (GitHub-only; default `false`) |

See [Configuration → Repository variables](docs/configuration.md#repository-variables) for the full list including the runtime tuning vars (`AI_REVIEW_MAX_DIFF_LINES`, `AI_REVIEW_MAX_INLINE`, `AI_REVIEW_MAX_TOKENS_PER_AGENT`, `AI_REVIEW_ENABLE_SUGGESTIONS`, `AI_REVIEW_PARALLEL`).

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

Once `ai-pr-review.yml` is merged to your default branch, users with write access can post commands on any PR:

| Command | Effect |
|---|---|
| `/ai-pr-review rescan` | Force full-diff re-review |
| `/ai-pr-review review-full` | Run all agents (full mode) |
| `/ai-pr-review skip` | Add `skip-ai-review` label |
| `/ai-pr-review help` | Post command list as reply |
| `/ai-pr-review dismiss [F<n>]` | Mark a finding a false positive. Reply on the inline comment thread, **or** post `dismiss F<n>` as a top-level comment using the `[F<n>]` ID shown on either an inline or body-level finding. Either way the matching inline thread is resolved, and the `CHANGES_REQUESTED` review is dismissed once every thread is resolved. |
| `/ai-pr-review false-positive [reason]` | Persist a false-positive verdict. Post as a reply on the AI's inline finding (recommended — also resolves the thread on success) **or** as a top-level PR comment. Requires `enable-feedback-loop: 'true'`. OWNER/MEMBER only. |
| `/ai-pr-review wont-fix [reason]` | Persist a "won't fix / by design" verdict. Same posting rules as `false-positive` (review-thread reply preferred). |
| `/ai-pr-review feedback <text>` | Persist free-form feedback for future review runs to consider. |
| `/ai-pr-review explain` | Ask the agent for a longer explanation (stub for now — replies with a canned message). |
| `/ai-pr-review revise <hint>` | Ask the agent to revise its verdict with a hint (stub for now). |

Slash commands are built into the canonical [examples/workflows/pr-review.yml](examples/workflows/pr-review.yml) template — copy that single file to `.github/workflows/ai-pr-review.yml` and both automatic review and slash commands are wired in one place. The `slash-commands` job calls a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) hosted here, so all command logic is maintained upstream. See [docs/slash-commands.md](docs/slash-commands.md) for details and the default-branch dispatch requirement.

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
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). Set to `false` to revert to sequential if you hit provider rate limits |
| `temperature` | No | `0.3` | Sampling temperature for LLM calls (float in [0, 2]). |
| `max-inline` | No | `25` | Maximum inline review comments per run; excess routed to the review body |
| `max-tokens-per-agent` | No | `16384` | Max output tokens per LLM agent call (clamped to [256, 65536]). Lowered from 32768 in v1.3.0. |
| `analyzer-concurrency` | No | `4` | Maximum simultaneous native static-analyzer subprocesses. Forced to 1 when `parallel: false`. |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab; ignored on Bitbucket). Set to `false` to disable. See [Code suggestions](#code-suggestions) |
| `engine` | No | `python` | Deprecated no-op. The bash engine was removed; Python is the only engine. Accepted for backward compatibility and ignored. |
| `ignore-merge-commits` | No | `true` | Strip base-branch merge commits before diff computation. Reviews only the PR author's own commits. Set to `false` to review all commits including upstream merges. |
| `context-enrichment` | No | `true` (container), `false` (direct action) | Inject tree-sitter symbol-context blocks into agent prompts. See [Opt-in capabilities](#opt-in-capabilities). |
| `sarif-paths` | No | `''` | Comma-separated SARIF 2.1.0 file paths to merge into findings. |
| `exclude-patterns` | No | `''` | Comma-separated git pathspec glob patterns to exclude from the diff before the LLM reads them (e.g. `docs/*,*.generated.go`). Reduces token cost on repos with large generated or vendored trees. The `":!"` prefix is added automatically. See `exclude-patterns-mode`. |
| `exclude-patterns-mode` | No | `append` | How `exclude-patterns` interacts with the built-in excludes (lockfiles, `vendor/`, `node_modules/`). `append` (default): user patterns are added after the built-ins. `replace`: only user patterns are used; built-in excludes are dropped. `replace` with an empty list falls back to the built-ins with a warning. |
| `analyzers` | No | `''` | Allowlist: comma-separated analyzer names to run. When set, only these analyzers run. Valid names: `shellcheck`, `trufflehog`, `semgrep`, `ruff`, `golangci-lint`, `hadolint`, `checkov`, `phpcs`, `phpstan`, `eslint`, `kube-linter`, `tflint`, `cve-check`. Empty (default): all eligible analyzers run. |
| `exclude-analyzers` | No | `''` | Denylist: comma-separated analyzer names to skip. Ignored when `analyzers` is set. Empty (default): no analyzers skipped. |
| `agents` | No | `''` | Allowlist: comma-separated agent names to run. When set, only these agents run (existing gates still apply). Valid names: `pr-summarizer`, `code-reviewer`, `silent-failure-hunter`, `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general`, `issue-linker`. Empty (default): all eligible agents run. |
| `exclude-agents` | No | `''` | Denylist: comma-separated agent names to skip. Ignored when `agents` is set. Note: excluding `pr-summarizer` suppresses the PR summary comment entirely. Empty (default): no agents skipped. |
| `analyzer-diff-scope` | No | `cap` | How out-of-diff native-analyzer findings are handled. `cap` (default): downgrade to Low severity and collapse into a `<details>` section so they don't trigger `REQUEST_CHANGES`. `drop`: remove out-of-diff analyzer findings entirely. `off`: pass through unchanged (full-file linting behaviour). LLM-agent findings are never affected. |
| `feedback-loop` | No | `false` | Persist `/ai-pr-review false-positive\|wont-fix\|feedback` verdicts to a dedicated git branch and re-inject them into future reviews. GitHub-only. |

Additional settings are available as **env-var-only** knobs for advanced tuning — see [docs/configuration.md](docs/configuration.md#advanced-tuning-env-var-only) for the full list (`FORCE_FULL_DIFF`, `STANDALONE_DEPTH`, `LLM_RETRY_COUNT`, `AI_CONFIDENCE_THRESHOLD`).

## Opt-in capabilities

Three optional features can be enabled independently — all off by default.

| Capability | Action input | Env var | Default | Description |
|-----------|-------------|---------|---------|-------------|
| **A. Context enrichment** | `context-enrichment: 'true'` | `AI_CONTEXT_ENRICHMENT=true` | `true` (container), `false` (direct action) | Use tree-sitter + ripgrep to look up cross-file symbol definitions referenced in the diff, then inject a `<symbol-context>` block (token-budget-capped) into eligible agent prompts. Reduces hallucinated "we should check X" findings by giving agents the real definitions. The container image ships both dependencies; direct-action consumers without them get a silent no-op. |
| **B. SARIF ingestion** | `sarif-paths: 'a.sarif,b.sarif'` | `AI_SARIF_PATHS=a.sarif,b.sarif` | `''` | Parse SARIF 2.1.0 files produced by external scanners (CodeQL, Semgrep, Trivy, Bandit, ...) and merge their findings into the same dedup/suppress/post pipeline as native analyzers. See [examples/workflows/sarif-codeql.yml](examples/workflows/sarif-codeql.yml). |
| **C. Learning loop** | `feedback-loop: 'true'` + `enable-feedback-loop: 'true'` on the slash-commands workflow | `AI_FEEDBACK_LOOP=true` | `false` | Reviewers post `/ai-pr-review false-positive`, `wont-fix`, or `feedback` to mark findings. Entries persist to a dedicated `ai-pr-review-bot` branch (auto-bootstrapped on first write) and feed into future agent prompts as a `<repo-feedback>` block. Requires `github-token` with `contents:write`. GitHub-only. See [docs/learning-loop.md](docs/learning-loop.md). |

See [docs/configuration.md](docs/configuration.md#opt-in-capabilities) for the full env-var reference including retention knobs (`AI_FEEDBACK_RETENTION_COUNT`, `AI_FEEDBACK_RETENTION_AGE_DAYS`), token budgets (`AI_CONTEXT_MAX_TOKENS`, `AI_FEEDBACK_MAX_TOKENS`), and the feedback branch name (`AI_FEEDBACK_BRANCH`).

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap — suitable for every push.

**Full mode**: Runs up to 8 agents — 6 always-on finding agents plus silent-failure-hunter (conditional) and pr-summarizer on first run. Trigger with the `ai-review-full` PR label, `workflow_dispatch` input, or by setting `review-mode: full` (optionally via an expression that auto-selects full mode for release branches).

For the full agent roster, trigger patterns, and auto-full-on-release workflow expressions (GitHub Actions and Bitbucket Pipelines), see [docs/agents.md](docs/agents.md#review-modes).

## Code suggestions

Code suggestions are enabled by default. The review tool asks eligible LLM agents to emit concrete code fixes alongside their findings. Each fix is rendered as a ```` ```suggestion ```` block inside the inline review comment, which GitHub and GitLab display as an "Apply suggestion" button — the PR/MR author can accept the fix with one click.

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

**How it works.** Eligible agents have a short prompt addendum appended to their system prompt instructing them to include a `suggested_code` field (and optional `start_line` for multi-line replacements) only when the fix is concrete and complete. The post-review script constructs the ```` ```suggestion ```` fence itself — agents are not trusted to emit the markdown directly. Multi-line suggestions are validated against the diff: every line in the replacement range must appear on the new-file side of a diff hunk, or the suggestion is dropped while keeping the natural-language remediation.

**Caveats.** Suggestions increase output token usage. The feature works on both GitHub and GitLab (using GitLab's `suggestion` fence syntax) — Bitbucket reviews ignore it. Suggestions are validated defensively: `start_line` must be a positive integer ≤ `line` with no leading zeros, multi-line ranges are capped at 100 lines, and `suggested_code` containing triple backticks (which would break the suggestion fence) is rejected. When any validation fails, the suggestion is dropped with a WARNING logged to the Actions run and the finding still posts with its natural-language remediation. On incremental reviews (SHA watermark active), suggestions only render when the finding's line range is still in the current incremental diff — add the `ai-review-rescan` label to force a full re-review.

## Incremental reviews

After the first full-PR review, subsequent pushes trigger an incremental review that only analyzes the new commits. The SHA watermark is stored in the summary comment and advanced after each review run.

If the watermark cannot be found (e.g., the summary comment was deleted), the action falls back to a full PR diff.

To force a full-PR diff for a single run, add the **`ai-review-rescan`** label to the PR. The watermark still advances normally afterward, so subsequent pushes resume incremental review — re-add the label if you want another full rescan.

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, etc.), the review continues with the remaining agents and notes which agents were skipped. If all finding agents fail, the review is aborted.

**LLM retries**: Transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient curl errors (connection refused, timeout, network failure) are retried with exponential backoff and jitter. Controlled by the `LLM_RETRY_COUNT` env var (default: 3).

**Parallel execution**: Agents run in a tiered fan-out by default — Tier 1 issues up to ~3 concurrent LLM calls alongside any triggered static analyzers; Tier 2 (full mode only) issues up to 5 concurrent LLM calls. The concurrency numbers apply to LLM calls only (for rate-limit planning); static analyzers run concurrently with them but do not consume LLM quota. If your provider's rate limits cannot sustain this throughput, set `parallel: false` to revert to sequential execution.

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

The action auto-detects languages from file extensions (19 language keys) and injects per-language context into agent prompts when a profile file exists. Language profiles are markdown files in `language-profiles/`:

| Profile file | Covers |
|---|---|
| `go.md` | Go |
| `php.md` | PHP / Drupal |
| `python.md` | Python |
| `shell.md` | Shell / Bash |
| `typescript.md` | TypeScript |
| `javascript.md` | JavaScript |
| `ruby.md` | Ruby / Rails |
| `rust.md` | Rust |
| `java.md` | Java |
| `c++.md` | C and C++ |
| `terraform.md` | Terraform / HCL |
| `yaml.md` | YAML (GitHub Actions, k8s, docker-compose) |
| `kotlin.md` | Kotlin |
| `swift.md` | Swift |
| `csharp.md` | C# / .NET |
| `scala.md` | Scala |
| `sql.md` | SQL |
| `lua.md` | Lua |
| `perl.md` | Perl |

To add a new language, create a `language-profiles/<language>.md` file. The filename (without extension) must match the lowercase language key returned by `detect_language()` in `ai_pr_review/languages.py` for the relevant file extensions. See CLAUDE.md for the full extension-to-language mapping.

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

To accept a specific CVE (e.g. a library used only in a test fixture), add a suppression rule matching the CVE or GHSA ID. See [docs/suppression.md](docs/suppression.md) for the schema and a worked example.

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
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | `CKV2_*` and `CKV_SECRET_*`→High; all other checks→Medium | 80 | `checkov` |
| **phpcs** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | `ERROR`→High, `WARNING`→Medium; Drupal+DrupalPractice standard when available, else PSR12 | 90 | `phpcs` |
| **eslint** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | severity 2→High, severity 1→Medium; uses consumer's config — no-op if no `eslint.config.*` or `.eslintrc.*` found | 90 | `eslint` |
| **phpstan** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | All findings→High; runs at level `PHPSTAN_LEVEL` (default 3) unless consumer has `phpstan.neon`/`phpstan.neon.dist` | 85 | `phpstan` |
| **kube-linter** | `.yaml`, `.yml`, `.json` with `apiVersion:` + `kind:` headers | All findings→Medium (reliability-focused: missing probes, resource limits, etc.) | 85 | `kube-linter` |
| **tflint** | `.tf`, `.tfvars` | `error`→High, `warning`→Medium, `notice`→Low; runs per Terraform module directory | 90 | `tflint` |

CVE check queries [OSV.dev](https://osv.dev/) against `go.mod`, `package.json`, `requirements.txt`, and `composer.json`. See [Dependency vulnerability check](#dependency-vulnerability-check) for details.

## Token usage

After each review run, a collapsible **Token usage by agent** table is appended to the summary comment. On incremental runs the table is refreshed in place — the first-run PR summary is preserved and only the token data is replaced. The table uses an adaptive column layout — when any agent reports cache activity (Anthropic explicit caching or OpenAI automatic prefix caching), the table expands to 8 columns:

| Column | Description |
|--------|-------------|
| Agent | Agent name |
| Model | Human-readable model name (e.g. "Sonnet 4.6", "GPT-4.1") |
| Input | Input tokens consumed (uncached) |
| Output | Output tokens generated |
| Cache Write | Tokens written to cache (Anthropic/Bedrock only; 0 for OpenAI) |
| Cache Read | Tokens read from cache (Anthropic explicit cache or OpenAI automatic prefix cache) |
| Total | Combined token count |
| Est. Cost | Estimated cost at public list prices |

When no cache activity is detected, the Cache Write and Cache Read columns are omitted (6-column layout).

Costs are calculated using rates from `config/model-pricing.json` and do not reflect enterprise discounts, committed use agreements, or proxy markups. The table is also written to the [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary) for easy access from the Actions run page.

## Architecture

At runtime, `python3 -m ai_pr_review review` (`ai_pr_review/cli.py`) computes the diff, dispatches LLM agents and static analyzers in parallel, merges and dedupes findings, then posts the summary and inline findings via the appropriate VCS provider module (`ai_pr_review/vcs/github.py`, `gitlab.py`, or `bitbucket.py`) and advances the SHA watermark.

For the directory layout, data-flow diagram, and dependency notes, see [docs/architecture.md](docs/architecture.md). For deep implementation internals (findings pipeline, caching, parallel execution, suggestions, test architecture), see [docs/architecture-internals.md](docs/architecture-internals.md).

## License

ai-pr-review is licensed under the [MIT License](LICENSE).

### Third-party tools

The container image redistributes third-party open-source analyzers (shellcheck,
hadolint, golangci-lint, trufflehog, tflint, semgrep, ruff, kube-linter, checkov, gh CLI,
phpstan, php_codesniffer, drupal/coder, ripgrep). Each retains its own license; full
texts and attribution are in [THIRD-PARTY-LICENSES/](THIRD-PARTY-LICENSES/NOTICE.md)
(also bundled at `/opt/ai-pr-review/THIRD-PARTY-LICENSES/` inside the image).

These tools run as separate, unmodified upstream processes; aggregating them in the image
does not place ai-pr-review under their copyleft terms. Notably, semgrep's use-restricted
registry rulesets are **not** bundled — semgrep fetches rules at runtime via
`--config=auto`. See [THIRD-PARTY-LICENSES/NOTICE.md](THIRD-PARTY-LICENSES/NOTICE.md) for
details and corresponding-source pointers.
