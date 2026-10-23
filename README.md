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

That's it: reviews start firing on the next PR.

**Going further:**
- [Slash commands](#slash-commands): `/ai-pr-review rescan`, `review-full`, `dismiss`, `fixed`, and learning-loop commands (`false-positive`, `wont-fix`, `feedback`), now built into the unified `pr-review.yml` template (no separate file needed)
- [Opt-in capabilities](#opt-in-capabilities): tree-sitter symbol-context enrichment (default on in the container image), SARIF 2.1.0 ingestion (CodeQL/Semgrep/Trivy), and the learning loop.
- [Installation](#installation): full-mode agents, provider configuration, [`examples/workflows/pr-review.yml`](examples/workflows/pr-review.yml) for the complete repo-variable pattern used by internal consumers

## Supported VCS providers

The same container image drives PR/MR reviews on GitHub, Bitbucket Cloud,
and GitLab. Select the provider via the `VCS_PROVIDER` env var (default: `github`).

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval |
|----------|---------------|---------|--------|-------------|----------|
| GitHub | `github` (default) | ✅ | ✅ | ✅ | ✅ |
| Bitbucket Cloud | `bitbucket` | ✅ | ✅ | ❌ | ✅ |
| GitLab | `gitlab` | ✅ | ✅ | ✅ | ✅ |

See [docs/bitbucket-setup.md](docs/bitbucket-setup.md) for Bitbucket Pipelines
setup and [docs/gitlab-setup.md](docs/gitlab-setup.md) for GitLab CI/CD setup
(token scopes, CI variables, starter pipeline, caveats). The remainder of
this README applies to the GitHub path.

## What it does

On every PR push, this action:

1. Computes the diff (full on first run, incremental on subsequent pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Runs deterministic checks on changed files: shellcheck, CVE lookups ([OSV.dev](https://osv.dev/)), semgrep SAST, trufflehog secret scanning, ruff (Python), golangci-lint (Go), hadolint (Dockerfiles), checkov (Terraform/K8s/IaC), phpcs (PHP/Drupal), eslint (JS/TS), dep-exists (new dependencies that no public registry knows), phpstan (PHP static analysis), kube-linter (Kubernetes), tflint (Terraform), and three documentation checks: doc-comment/signature mismatch, broken Markdown links/anchors, and stale references to deleted files
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

Findings use these icons:

| Icon | Severity | Review action |
|------|----------|---------------|
| ⛔ | Critical | REQUEST_CHANGES |
| 🚨 | High | REQUEST_CHANGES |
| 🔶 | Medium | APPROVE (informational) |
| 💬 | Low | APPROVE (informational) |

The "Review action" column above is this bot's default behavior. Set the `approval-ceiling` input if you never want it to post a real APPROVE (or, at the strictest setting, never set any formal review state at all). A human then makes every merge decision. See [Configuration: Approval ceiling](docs/configuration.md#approval-ceiling).

## Supported LLM providers

| Provider | `provider` value | Required secret | Default models (standard / premium) |
|----------|-----------------|-----------------|--------------------------------------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` / `claude-opus-5-5` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-6-luna` / `gpt-6.1-sol` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-3.5-flash-lite` / `gemini-3.8-flash` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-5` / `global.anthropic.claude-opus-4-7` |

### Provider-specific notes

**Anthropic**: The default and most-tested provider. Uses explicit prompt caching (`cache_control: ephemeral`) with a shared-cache layout that gives agents in the same cohort a single shared cache entry. In a May 2026 benchmark on Sonnet 4.6, caching cut cost by about 46% on a cold run and about 61% on a hot run. Nobody has measured the current layout. Premium tier uses Opus for deeper reasoning on Tier 2 agents (architecture, security, edge-case).

**OpenAI**: Fully supported. Default models are `gpt-6-luna` (standard, Tier 1) and `gpt-6.1-sol` (premium, Tier 2). Uses `max_completion_tokens`, the modern field (`max_tokens` is kept for `openai-compatible`). Automatic prefix caching (discounted cached input tokens for prompts of 1024 tokens or more) is maximized by a shared-cache request layout that puts the shared review context first in the system message, so agents in the same cohort share a common prefix. Cache hits are measured and reported in the token usage table. Reasoning models (`o3`, `o4-mini`) and `gpt-5`, `gpt-5.5`, `gpt-5.6-*` and `gpt-6*` are supported, and temperature is omitted for them automatically. It is still sent to `gpt-5.4` and `gpt-5.4-mini`. An earlier benchmark on a 162-line diff found the previous defaults (`gpt-5.4-mini` + `gpt-5.4`) about 79% cheaper than Anthropic (Sonnet + Opus) with comparable finding quality. That benchmark has not been repeated for the GPT-6 defaults. To keep the previous defaults, set `model-standard: gpt-5.4-mini` and `model-premium: gpt-5.4`. `gpt-4o`, `gpt-4.1`, `gpt-5.5`, the `gpt-5.6-*` models, `gpt-6-sol`, and `gpt-6-astra` are also supported.

**OpenAI-compatible**: For third-party endpoints (Azure OpenAI, Groq, Together, local models). Uses the legacy `max_tokens` field for broader compatibility. Requires `base-url` and explicit `model-standard`/`model-premium` inputs.

**Google Gemini**: Fully supported. Default models are `gemini-3.5-flash-lite` (standard) and `gemini-3.8-flash` (premium). Google now serves the Gemini 2.5 models only to projects that have used them before, so new projects need a 3.x model. There is no stable Gemini 3.x Pro model yet, which is why the premium default is a Flash model. Temperature is omitted for Gemini 3 models, because Google advises keeping them at the default of 1.0. Thinking is capped at `low` for Gemini 3 models other than Flash-Lite, because the output limit includes thinking tokens and the default level came close to using all of it. Thinking tokens are billed at the output rate. They are extracted, added to the output count for cost estimation, and logged as `THINKING: N tokens` on stderr. Implicit caching (`cachedContentTokenCount`) is extracted when present. `gemini-3.8-flash` has introductory pricing ($0.75 / $3.75 per MTok) through 2026-12-31, and the price doubles to $1.50 / $7.50 on 2027-01-01. The bundled pricing table already uses the 2027 rate, so cost estimates and `AI_MAX_COST_USD` do not under-count. Projects that still have access to the 2.5 models can keep them with `model-standard: gemini-2.5-flash` and `model-premium: gemini-2.5-pro`.

## Requirements

**The container action is the recommended way to run ai-pr-review.** It pulls a public multi-arch image from GHCR (linux/amd64 and linux/arm64). No additional authentication or toolchain setup required. Most analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint) ship pre-installed at pinned versions. ESLint is not bundled (it runs from the consumer's `node_modules` / `npx` via the project's own config); the review proceeds without ESLint findings if no JS toolchain is present.

If you prefer to run without Docker (e.g., on self-hosted runners without container support), the [direct action reference](docs/installation-direct-action.md) and [git submodule](docs/installation-submodule.md) methods work as standard GitHub Actions composite actions. These require:

- **Python 3.11 or later** (with `pip`), **git**, **gh**: `git` and `gh` are pre-installed on standard GitHub-hosted runners
- **shellcheck**: installed automatically by the action if not already present
- Other static-analyzer binaries installed separately if desired (see [runtime dependencies](docs/installation-direct-action.md#runtime-dependencies))

Both methods require:

- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repos; see [installation notes](#installation) for exceptions)
- An API key for one of the [supported LLM providers](#supported-llm-providers)

## Installation

The container action is the recommended installation method: it ships all analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint) pre-installed at pinned, verified versions. The image supports linux/amd64 and linux/arm64 natively (Apple Silicon, Graviton runners). No toolchain setup on your runner. See [Quickstart](#quickstart) for the minimal two-step setup.

### Full setup

The example workflow in [examples/workflows/pr-review.yml](examples/workflows/pr-review.yml) uses the container action:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  env:
    FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
  with:
    image-tag: ${{ vars.AI_REVIEW_IMAGE_TAG || 'latest' }}  # or pin to a release tag, e.g. '2.25.0'
    provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
    api-key: ${{ secrets.AI_REVIEW_API_KEY }}
    base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
    review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || '' }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-ref: ${{ github.event.pull_request.head.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

See [examples/README.md](examples/README.md) for the complete setup walkthrough including [`examples/workflows/pr-review.yml`](examples/workflows/pr-review.yml), the canonical template every internal Tag1 repo uses, with every input wired to a `vars.AI_REVIEW_*` repo variable with a safe fallback default.

**Secrets and variables**: configure in the consuming repository's settings. All variables are optional; the secret is required.

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `AI_REVIEW_API_KEY` | Secret | Yes | API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | Variable | No | Provider name (default: `anthropic`) |
| `AI_REVIEW_BASE_URL` | Variable | No | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Variable | No | Override the standard model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Variable | No | Override the premium model ID (full mode only) |
| `AI_REVIEW_IMAGE_TAG` | Variable | No | Container image tag (default `latest`; set to `dev` to dogfood pre-release builds or pin to a release like `2.25.0`: image tags published by `publish-image.yml` strip the `v` prefix) |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | Variable | No | `false` to disable stripping of base-branch merge commits from the diff before review (default `true`) |
| `AI_REVIEW_CONTEXT_ENRICHMENT` | Variable | No | `true` to enable tree-sitter symbol-context injection (default `true` for the container action, `false` for the direct action) |
| `AI_REVIEW_SARIF_PATHS` | Variable | No | Comma-separated SARIF 2.1.0 file paths to merge as findings (default `''`) |
| `AI_REVIEW_FEEDBACK_LOOP` | Variable | No | `true` to enable the learning loop (GitHub + Bitbucket; default `false`) |

See [Configuration → Repository variables](docs/configuration.md#repository-variables) for the full list including the runtime tuning vars (`AI_REVIEW_MAX_DIFF_LINES`, `AI_REVIEW_MAX_INLINE`, `AI_REVIEW_MAX_TOKENS_PER_AGENT`, `AI_REVIEW_ENABLE_SUGGESTIONS`, `AI_REVIEW_PARALLEL`).

**Local development**: run reviews against any open PR without a CI runner:

```bash
# Dry run: builds the review and prints a short status, does not post to GitHub
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

- **[Direct action reference](docs/installation-direct-action.md)**: uses the root composite action directly, without Docker. Installs shellcheck automatically; does not install semgrep, trufflehog, ruff, or golangci-lint.
- **[Git submodule](docs/installation-submodule.md)**: explicit, auditable version pinning; commits the exact action source into your repository. Uses a 3-job pattern to isolate the PAT used for submodule checkout.

## Slash commands

Once `ai-pr-review.yml` is merged to your default branch, users with write access can post commands on any PR:

| Command | Effect |
|---|---|
| `/ai-pr-review rescan` | Force full-diff re-review |
| `/ai-pr-review review-full` | Run all agents (full mode). `/ai-pr-review full` is an alias |
| `/ai-pr-review skip` | Add `skip-ai-review` label |
| `/ai-pr-review help` | Post command list as reply |
| `/ai-pr-review dismiss [F<n>]` | Mark a finding a false positive. Reply on the inline comment thread, **or** post `dismiss F<n>` as a top-level comment using the `[F<n>]` ID shown on either an inline or body-level finding. Either way the matching inline thread is resolved, and the `CHANGES_REQUESTED` review is dismissed once every thread is resolved. If that clears the last active finding PR-wide and the actor is OWNER/MEMBER, the PR is also approved. See [Auto-approve on clear](docs/slash-commands.md#auto-approve-on-clear). |
| `/ai-pr-review false-positive [reason]` | Persist a false-positive verdict. Post as a reply on the AI's inline finding (recommended: resolves the thread and dismisses the owning review on success, same mechanics as `dismiss`) **or** as a top-level PR comment. Requires `enable-feedback-loop: 'true'`. OWNER/MEMBER only. |
| `/ai-pr-review wont-fix [reason]` | Persist a "won't fix / by design" verdict. Same posting rules and dismissal mechanics as `false-positive` (review-thread reply preferred). |
| `/ai-pr-review fixed [F<n>] [sha]` | Mark a finding as fixed in code: the opposite claim from `dismiss`/`false-positive`/`wont-fix`. Same resolution/dismissal mechanics as `dismiss`, but never triggers the auto-approve escalation (a fix claim isn't a verified fix) and never writes to the feedback store (it isn't a verdict the finding was wrong). The optional commit SHA is echoed bare in the reply so GitHub auto-links it; it's never validated. |
| `/ai-pr-review feedback <text>` | Persist free-form feedback for future review runs to consider. |
| `/ai-pr-review explain` | Ask the agent for a longer explanation (stub for now: replies with a canned message). |
| `/ai-pr-review revise <hint>` | Ask the agent to revise its verdict with a hint (stub for now). |

Slash commands are built into the canonical [examples/workflows/pr-review.yml](examples/workflows/pr-review.yml) template. Copy that single file to `.github/workflows/ai-pr-review.yml` and both automatic review and slash commands are wired in one place. The `slash-commands` job calls a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) hosted here, so all command logic is maintained upstream. See [docs/slash-commands.md](docs/slash-commands.md) for details and the default-branch dispatch requirement.

## Fork pull requests

By default (`AI_FORK_POLICY=auto`), the engine reviews a fork pull request at once, with no maintainer step. A non-trusted author gets a limited run: a scrubbed environment, fewer analyzers, no PR body in the prompts, a COMMENT-only output, and spend caps. Set `AI_FORK_POLICY=label` to require a maintainer approval of each commit, or `block` to switch fork review off. The checkout step must set `persist-credentials: false`. See [docs/fork-prs.md](docs/fork-prs.md) and [SECURITY.md](SECURITY.md).

## Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `anthropic` | LLM provider |
| `api-key` | **Yes** | none | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `''` | `quick` or `full`. Empty (default): use the `policy.yml` route if one matches, else `quick`. |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone`. Standalone currently only disables merge-commit filtering during diff computation: it does not post findings anywhere; issue-posting was part of the bash engine removed in v2.0.0 and has not been reimplemented. |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | none | Base branch name |
| `head-sha` | **Yes** | none | Head commit SHA |
| `policy-source` | No | `base-ref` | Where `.ai-pr-review/policy.yml` (or its `.github/ai-pr-review/policy.yml` fallback) is read from (`base-ref` or `workspace`). On Bitbucket the verdict owners file `.bitbucket/CODEOWNERS` does not follow this setting. It is always read from the target branch. See [Configuration: Action inputs](docs/configuration.md#action-inputs) and [Policies: Security](docs/policy.md) before setting `workspace`. |
| `github-token` | **Yes** | none | GitHub token with `pull-requests: write` |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). Set to `false` to revert to sequential if you hit provider rate limits |
| `temperature` | No | `0.3` | Sampling temperature for LLM calls (float in [0, 2]). Not sent to models that reject or discourage a non-default value (currently Claude Opus 4.7, 4.8, 5 and 5.5, Claude Sonnet 5 and 5.5, Claude Haiku 5.5, OpenAI `o1`/`o3`/`o4`, `gpt-5`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`, and Gemini 3), where the provider's default applies. `gpt-5.4` and `gpt-5.4-mini` still receive it. The exact list is `resolve_temperature()` in `ai_pr_review/llm/_config.py`. |
| `max-inline` | No | `25` | Maximum inline review comments per run; excess routed to the review body |
| `max-tokens-per-agent` | No | `32768` | Max output tokens per LLM agent call (clamped to [256, 65536]). Lowered from 32768 to 16384 in v1.3.0, raised back for #642. Overridable per agent via `AI_MAX_TOKENS_<AGENT_NAME>` (e.g. `AI_MAX_TOKENS_CODE_REVIEWER`), higher-precedence than this global value; see [Configuration: Per-agent max-tokens overrides](docs/configuration.md#per-agent-max-tokens-overrides-env-var-only). |
| `analyzer-concurrency` | No | `4` | Maximum simultaneous native static-analyzer subprocesses. Forced to 1 when `parallel: false`. |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab; ignored on Bitbucket). Set to `false` to disable. See [Code suggestions](#code-suggestions) |
| `engine` | No | `python` | Deprecated no-op. The bash engine was removed; Python is the only engine. Accepted for backward compatibility and ignored. |
| `ignore-merge-commits` | No | `true` | Strip base-branch merge commits before diff computation. Reviews only the PR author's own commits. Set to `false` to review all commits including upstream merges. |
| `context-enrichment` | No | `true` (container), `false` (direct action) | Inject tree-sitter symbol-context blocks into agent prompts. See [Opt-in capabilities](#opt-in-capabilities). |
| `sarif-paths` | No | `''` | Comma-separated SARIF 2.1.0 file paths to merge into findings. |
| `exclude-patterns` | No | `''` | Comma-separated git pathspec glob patterns to exclude from the diff before the LLM reads them (e.g. `docs/*,*.generated.go`). Reduces token cost on repos with large generated or vendored trees. The `":!"` prefix is added automatically. See `exclude-patterns-mode`. |
| `exclude-patterns-mode` | No | `append` | How `exclude-patterns` interacts with the built-in excludes (lockfiles, `vendor/`, `node_modules/`). `append` (default): user patterns are added after the built-ins. `replace`: only user patterns are used; built-in excludes are dropped. `replace` with an empty list falls back to the built-ins with a warning. |
| `analyzers` | No | `''` | Allowlist: comma-separated analyzer names to run. When set, only these analyzers run. Valid names: `shellcheck`, `trufflehog`, `semgrep`, `ruff`, `golangci-lint`, `hadolint`, `checkov`, `phpcs`, `phpstan`, `eslint`, `kube-linter`, `tflint`, `cve-check`, `dep-exists`, `docs-api-check`, `docs-ref-check`, `docs-drift-check`, `suppression-check`. `docs-missing-check` is also accepted as a deprecated no-op (issue #815, removed for real in v3.0.0). Empty (default): all eligible analyzers run. |
| `exclude-analyzers` | No | `''` | Denylist: comma-separated analyzer names to skip. Ignored when `analyzers` is set. Empty (default): no analyzers skipped. |
| `agents` | No | `''` | Allowlist: comma-separated agent names to run. When set, only these agents run (existing gates still apply). Valid names: `pr-summarizer`, `code-reviewer`, `silent-failure-hunter`, `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general`, `issue-linker`. Empty (default): all eligible agents run. |
| `exclude-agents` | No | `''` | Denylist: comma-separated agent names to skip. Ignored when `agents` is set. Note: excluding `pr-summarizer` suppresses the PR summary comment entirely. Empty (default): no agents skipped. |
| `analyzer-diff-scope` | No | `cap` | How out-of-diff native-analyzer findings are handled. `cap` (default): downgrade to Low severity and collapse into a `<details>` section so they don't trigger `REQUEST_CHANGES`. `drop`: remove out-of-diff analyzer findings entirely. `off`: pass through unchanged (full-file linting behaviour). LLM-agent findings are never affected. |
| `feedback-loop` | No | `false` | Persist `/ai-pr-review false-positive\|wont-fix\|feedback` verdicts to a dedicated git branch and re-inject them into future reviews. GitHub and Bitbucket. |
| `finding-badges` | No | `true` | Show an evidence label and the confidence score beside each finding, for example `(judge kept, confidence 80)`. Display only, it never changes the approval decision. Set `false` to hide it. See [`AI_FINDING_BADGES`](docs/configuration.md). |
| `suppress-walkthrough` | No | `false` | Leave the Walkthrough table out of the summary comment. The Summary, Type and Effort stay. See [`AI_SUPPRESS_WALKTHROUGH`](docs/configuration.md). |
| `judge-pass` | No | `true` | Run a cheap-model judge pass after findings are extracted to down-rank weak single-source findings. Adds one LLM call per review; its token usage appears as a `judge-pass` row in the token table. Set to `false` to disable. |
| `profile-max-tokens` | No | `4096` | Deprecated, ignored (#814): per-agent language-profile routing was removed; every eligible agent now receives the whole detected-language profile(s) instead. Accepted as a no-op with a deprecation warning; will be rejected starting in v3.0.0. |

Additional settings are available as **env-var-only** knobs for advanced tuning. See [docs/configuration.md](docs/configuration.md#advanced-tuning-env-var-only) for the full list (`FORCE_FULL_DIFF`, `STANDALONE_DEPTH`, `LLM_RETRY_COUNT`, `AI_CONFIDENCE_THRESHOLD`).

## Opt-in capabilities

Four optional capabilities can be set independently. The Default column shows the state of each one.

| Capability | Action input | Env var | Default | Description |
|-----------|-------------|---------|---------|-------------|
| **A. Context enrichment** | `context-enrichment: 'true'` | `AI_CONTEXT_ENRICHMENT=true` | `true` (container), `false` (direct action) | Use tree-sitter + ripgrep to look up cross-file symbol definitions referenced in the diff, then inject a `<symbol-context>` block (token-budget-capped) into eligible agent prompts. Reduces hallucinated "we should check X" findings by giving agents the real definitions. The container image ships both dependencies; direct-action consumers without them get a silent no-op. |
| **B. SARIF ingestion** | `sarif-paths: 'a.sarif,b.sarif'` | `AI_SARIF_PATHS=a.sarif,b.sarif` | `''` | Parse SARIF 2.1.0 files produced by external scanners (CodeQL, Semgrep, Trivy, Bandit, ...) and merge their findings into the same dedup/suppress/post pipeline as native analyzers. See [examples/workflows/sarif-codeql.yml](examples/workflows/sarif-codeql.yml). |
| **C. Learning loop** | `feedback-loop: 'true'` + `enable-feedback-loop: 'true'` on the slash-commands workflow | `AI_FEEDBACK_LOOP=true` | `false` | Reviewers post `/ai-pr-review false-positive`, `wont-fix`, or `feedback` (GitHub and Bitbucket) to mark findings. Entries persist to a dedicated `ai-pr-review-bot` branch (auto-bootstrapped on first write) and feed into future agent prompts as a `<repo-feedback>` block. Requires `github-token` with `contents:write` on GitHub, or an API token with **Repository:Write** on Bitbucket (see [docs/bitbucket-setup.md](docs/bitbucket-setup.md) for the security tradeoffs of that scope). See [docs/learning-loop.md](docs/learning-loop.md). |
| **D. Judge pass** | `judge-pass: 'false'` to disable | `AI_JUDGE_PASS=false` | `true` | After findings are extracted, a single cheap-model call scores each candidate finding and down-ranks weak single-source results (lowers confidence, routes to review body instead of inline). Corroborated findings (static-analyzer + LLM-agent agreement on the same location) are exempt. Adds one LLM call per review; its token cost appears as a `judge-pass` row in the token table. Always fail-soft: a judge error returns findings unchanged. |

See [docs/configuration.md](docs/configuration.md#opt-in-capabilities) for the full env-var reference including retention knobs (`AI_FEEDBACK_RETENTION_COUNT`, `AI_FEEDBACK_RETENTION_AGE_DAYS`), token budgets (`AI_CONTEXT_MAX_TOKENS`, `AI_FEEDBACK_MAX_TOKENS`, `AI_PROFILE_MAX_TOKENS`), and the feedback branch name (`AI_FEEDBACK_BRANCH`).

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap, suitable for every push.

**Full mode**: Runs up to 9 agents: 6 finding agents, plus silent-failure-hunter (conditional), pr-summarizer (first run), and issue-linker (GitHub only). Trigger with the `ai-review-full` PR label, `workflow_dispatch` input, `review-mode: full`, or by routing to it via `.ai-pr-review/policy.yml` (e.g. full mode for release branches, quick mode elsewhere).

For the full agent roster and trigger patterns, see [docs/agents.md](docs/agents.md#review-modes). For per-branch/per-path routing, see [docs/policy.md](docs/policy.md).

## Code suggestions

Code suggestions are enabled by default. The review tool asks eligible LLM agents to emit concrete code fixes alongside their findings. Each fix is rendered as a ```` ```suggestion ```` block inside the inline review comment, which GitHub and GitLab display as an "Apply suggestion" button. The PR/MR author can accept the fix with one click.

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

**How it works.** Eligible agents have a short prompt addendum appended to their system prompt instructing them to include a `suggested_code` field (and optional `start_line` for multi-line replacements) only when the fix is concrete and complete. The post-review script constructs the ```` ```suggestion ```` fence itself. Agents are not trusted to emit the markdown directly. Multi-line suggestions are validated against the diff: every line in the replacement range must appear on the new-file side of a diff hunk, or the suggestion is dropped while keeping the natural-language remediation.

**Caveats.** Suggestions increase output token usage. The feature works on both GitHub and GitLab (using GitLab's `suggestion` fence syntax). Bitbucket reviews ignore it. Suggestions are validated defensively: `start_line` must be a positive integer ≤ `line` with no leading zeros, multi-line ranges are capped at 100 lines, and `suggested_code` containing triple backticks (which would break the suggestion fence) is rejected. When any validation fails, the suggestion is dropped with a WARNING logged to the Actions run and the finding still posts with its natural-language remediation. On incremental reviews (SHA watermark active), suggestions only render when the finding's line range is still in the current incremental diff. Add the `ai-review-rescan` label to force a full re-review.

## Incremental reviews

After the first full-PR review, subsequent pushes trigger an incremental review that only analyzes the new commits. The SHA watermark is stored in the summary comment and advanced after each review run.

If the watermark cannot be found (e.g., the summary comment was deleted), the action falls back to a full PR diff.

To force a full-PR diff for a single run, add the **`ai-review-rescan`** label to the PR. The watermark still advances normally afterward, so subsequent pushes resume incremental review. Re-add the label if you want another full rescan.

## Quiet reruns (GitHub)

Rerunning the review no longer always posts a new review object. Each run classifies its findings against the bot's most-recently-posted review and its existing threads: nothing new updates that review's body in place with no new Conversation-tab entry; a still-open finding is updated (and, on a severity increase, gets a reply) without a new review; a `fixed` finding that recurs gets a reply and its thread reopened; a `dismiss`/`false-positive`/`wont-fix`'d finding is never reposted; only a genuinely new (or too-severe-to-hide) finding triggers a fresh review, carrying just that finding, and even then, the prior blocking review is only dismissed once none of its own findings are still open. See [docs/features.md](docs/features.md#quiet-reruns-github) for the full decision table. GitHub-only for now (GitLab/Bitbucket parity tracked in issue #710).

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, etc.), the review continues with the remaining agents and notes which agents were skipped. If all finding agents fail, the review is aborted.

**LLM retries**: Transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient network errors (connection refused, timeout, network failure) are retried with exponential backoff and jitter. Controlled by the `LLM_RETRY_COUNT` env var (default: 3).

**Parallel execution**: Agents run in a tiered fan-out by default: up to 4 LLM calls run at the same time (one shared limit for both tiers), alongside any triggered static analyzers. The concurrency limit applies to LLM calls only (for rate-limit planning); static analyzers run concurrently with them but do not consume LLM quota. If your provider's rate limits cannot sustain this throughput, set `parallel: false` to revert to sequential execution.

**GitHub API retries**: Critical GitHub API calls (posting reviews, comments) retry on 429, 502, 503, and 504 and on transient network errors, with exponential backoff and jitter.

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
- `file`: Substring match on the finding's file path
- `line`: Exact line number match
- `code`: Finding text starts with this prefix
- `pattern`: Regex matched against the finding text

### Local suppressions

Consuming repos can add their own suppression rules without modifying the action. Create `.ai-pr-review/suppressions.json` in your repository using the same schema (`.github/ai-pr-review/suppressions.json` still works as a fallback for existing adopters):

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

Local rules are merged with the global suppression rules at runtime. No action input or configuration is required.

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

The check also reads lockfiles, and a lockfile wins over a range manifest when both are present: `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `Pipfile.lock`, `uv.lock`, `composer.lock`, `Cargo.lock`, and `Gemfile.lock`.

Findings are mapped from CVSS score: ≥ 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, below 4.0 or unscored → Low. Critical and High findings trigger `REQUEST_CHANGES` on the PR review just like any other high-severity finding.

No configuration is required: the check runs automatically when a manifest file is in the diff. The OSV.dev API is unauthenticated and free. If the API is unreachable, the check emits a warning and continues. The review is never blocked by CVE-lookup failures.

To accept a specific CVE (e.g. a library used only in a test fixture), add a suppression rule matching the CVE or GHSA ID. See [docs/suppression.md](docs/suppression.md) for the schema and a worked example.

## Static analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path and fall back to sequential when `parallel: false`. If a binary is missing, the wrapper script emits a WARNING to stderr and returns `[]`. The review is never blocked.

The container action ships all analyzer binaries pre-installed. For the direct-action or submodule paths, install the binaries you need; see [docs/installation-direct-action.md](docs/installation-direct-action.md#runtime-dependencies).

| Analyzer | Language gate | Severity mapping | Confidence | Source tag |
|----------|--------------|-----------------|------------|------------|
| **shellcheck** | `.sh`, `.bash` | `error`→High, `warning`→Medium | 95 | `shellcheck` |
| **semgrep** | Any file | `ERROR`→High, `WARNING`→Medium, else→Low | 90 | `semgrep` |
| **trufflehog** | Any file | Verified secret→Critical, Unverified→High (an unverified secret in a test file→Low) | 95 / 85 | `trufflehog` |
| **ruff** | `.py` files | `F`/`E` prefix→High, `W`/`C`→Medium, else→Low | 90 | `ruff` |
| **golangci-lint** | `.go` files | `errcheck`/`govet`/`staticcheck`→High, others→Medium | 90 | `golangci-lint` |
| **hadolint** | `Dockerfile*`, `*.dockerfile` | `error`→High, `warning`→Medium, else→Low | 90 | `hadolint` |
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | `CKV2_*` and `CKV_SECRET_*`→High; all other checks→Medium | 80 | `checkov` |
| **phpcs** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | `ERROR`→High, `WARNING`→Medium; honors the repo phpcs ruleset's file scope and installed rules (read as data, never passed to phpcs), Drupal+DrupalPractice only for Drupal repos, else PSR12 | 90 | `phpcs` |
| **eslint** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | severity 2→High, severity 1→Medium; uses consumer's config, no-op if no `eslint.config.*` or `.eslintrc.*` found | 90 | `eslint` |
| **phpstan** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | Severity follows `PHPSTAN_LEVEL` (8-9 High, 5-7 Medium, 0-4 Low); always runs at level `PHPSTAN_LEVEL` (default 3). A project's own `phpstan.neon`/`phpstan.neon.dist` is never auto-discovered, since the analyzed workspace may be untrusted fork-PR content | 85 | `phpstan` |
| **kube-linter** | `.yaml`, `.yml`, `.json` with `apiVersion:` + `kind:` headers | Security checks such as `privileged-container` and `host-network`→High, all others→Medium (reliability-focused: missing probes, resource limits, etc.) | 85 | `kube-linter` |
| **tflint** | `.tf`, `.tfvars` | `error`→High, `warning`→Medium, `notice`→Low; runs per Terraform module directory | 90 | `tflint` |

CVE check queries [OSV.dev](https://osv.dev/) against `go.mod`, `package.json`, `requirements.txt`, and `composer.json`. See [Dependency vulnerability check](#dependency-vulnerability-check) for details.

## Token usage

By default, each posted review comment carries a single compact line summarizing the run's cost, e.g.:

> _Review cost: $0.1234 · 45,678 tokens · 8 agents · Sonnet 5 · [full breakdown](https://github.com/OWNER/REPO/actions/runs/RUN_ID)_

The full **Token usage by agent** table (the default before this feature shipped) is always available in two other places, regardless of `token-usage-display`:

- The CI job log: echoed to stderr on every run, on every provider.
- The [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary), on GitHub only.

Set `token-usage-display: full` to restore the full table inside the review comment itself, or `off` to omit token-usage content from the comment entirely. See [Configuration](docs/configuration.md) for both inputs, including `token-usage-warn-usd` (default `1.00`), which adds a separate warning line to the comment when a run's estimated cost crosses the configured threshold.

A review also stops before any LLM call when the diff is too large for the model context window. The engine compares the estimated tokens of each agent with `AI_CONTEXT_WINDOW_MARGIN` (default `0.9`) times the `context_window` of the model in `config/model-pricing.json`. A skip comment names the agent and the numbers. `max-diff-lines` counts lines and not tokens, so a diff of generated or minified files can pass it and still be too large. Use `AI_EXCLUDE_PATTERNS` for those files. See [Configuration](docs/configuration.md) for `AI_CONTEXT_WINDOW_MARGIN` and `AI_FAIL_ON_CONTEXT_WINDOW`.

Set `max-cost-usd` to a positive value (default `0`, disabled) to enforce a ceiling: before any agent dispatches, the run's cost is estimated from the diff size, agent roster, and model pricing, logged as a `COST_ESTIMATE` line, and, if it exceeds the ceiling, the run aborts before any LLM call is made, posting a skip comment instead. That skip comment still carries the findings from native static analyzers and SARIF ingestion, which run before this check and cost nothing to run. They're suppressed and diff-scoped the same way a normal run's findings are. By default this exits 0 (informational: one large PR shouldn't break a required check); set `fail-on-cost-ceiling: true` to exit 2 instead, or rely on `fail-on-findings`, which independently exits 2 if the skip's surviving findings would block approval. A model with no pricing entry is estimated at $0 and so is not bounded by the ceiling: by default the review runs and says so in the review comment, and `cost-ceiling-unpriced: block` skips it instead. See [Configuration: Cost ceiling](docs/configuration.md#cost-ceiling) for the estimation methodology and its known imprecision.

The full table (comment under `full`, step summary, and job log) uses an adaptive column layout. When any agent reports cache activity (Anthropic explicit caching or OpenAI automatic prefix caching), the table expands to 8 columns:

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

When `AI_JUDGE_PASS=true` (the default) and the judge ran on a non-empty finding set, a `judge-pass` row appears as a regular agent row with its tokens included in the Total. Three supplementary informational rows may appear after Total (they do not affect cost totals):

- **Context enrichment**: token count of the `<symbol-context>` block; shown when `AI_CONTEXT_ENRICHMENT=1` and the block was non-empty.
- **Language profiles**: token count of the whole detected-language profile text injected into every eligible agent (#814); shown when at least one language profile was loaded.
- **SARIF ingestion**: wall-clock elapsed time for SARIF parsing; shown when `AI_SARIF_PATHS` is configured.

Costs are calculated using rates from `config/model-pricing.json` and do not reflect enterprise discounts, committed use agreements, or proxy markups.

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
registry rulesets are **not** bundled: semgrep fetches rules at runtime via
`--config=auto`. See [THIRD-PARTY-LICENSES/NOTICE.md](THIRD-PARTY-LICENSES/NOTICE.md) for
details and corresponding-source pointers.
