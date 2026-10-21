---
layout: default
title: Direct Action Reference
parent: Configuration
nav_order: 8
render_with_liquid: false
---

# Installation: Direct action reference

Use this installation method to run without Docker. The root composite action installs shellcheck on the runner automatically. It does **not** install other static analyzer binaries (semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint, or eslint). See [Static analyzers](static-analyzers) for details. Each analyzer does nothing, without an error, when its binary is absent.

For most new installs, use the [container action](getting-started). It ships the analyzer binaries pre-installed at pinned versions.

## Prerequisites

The `tag1consulting/ai-pr-review` repository is public. You do not need to change any Actions access setting to use it as an action.

If you use a private fork or mirror, set its access in **Settings → Actions → General → Access**. Choose the option that lets your other repositories use the action.

## 1. Create the workflow

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
    # The head.repo.full_name check is defense in depth against fork PRs.
    # Fork PRs cannot access secrets anyway. They also should not start review
    # jobs that hold pull-requests: write.
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
        env:
          FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          model-standard: ${{ vars.AI_REVIEW_MODEL_STANDARD || '' }}
          model-premium: ${{ vars.AI_REVIEW_MODEL_PREMIUM || '' }}
          review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || '' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          max-diff-lines: ${{ vars.AI_REVIEW_MAX_DIFF_LINES || '5000' }}
          max-inline: ${{ vars.AI_REVIEW_MAX_INLINE || '25' }}
          max-tokens-per-agent: ${{ vars.AI_REVIEW_MAX_TOKENS_PER_AGENT || '32768' }}
          enable-suggestions: ${{ vars.AI_REVIEW_ENABLE_SUGGESTIONS || 'true' }}
          parallel: ${{ vars.AI_REVIEW_PARALLEL || 'true' }}
          ignore-merge-commits: ${{ vars.AI_REVIEW_IGNORE_MERGE_COMMITS || 'true' }}
          fail-on-findings: ${{ vars.AI_REVIEW_FAIL_ON_FINDINGS || 'true' }}

  # Always try to remove the ai-review-rescan label after the review.
  # This also runs if the concurrency rule cancelled the review job on a new push.
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

The snippet above uses the `@v4` tag for readability. To pin third-party actions to commit SHAs (recommended), copy [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml). Renovate keeps that file pinned and up to date.

To pin a specific version, replace `@main` with a tag or commit SHA (for example `@v2.24.1` or `@cb9d7ee`).

## 2. Configure secrets and variables

In the **consuming** repository's settings:

**Secrets:**
- `AI_REVIEW_API_KEY`: API key for your chosen LLM provider

**Variables** (optional): Set these in Settings → Secrets and variables → Actions → Variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_REVIEW_PROVIDER` | `anthropic` | Provider name |
| `AI_REVIEW_BASE_URL` | `''` | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Per-provider default | Override the standard model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Per-provider default | Override the premium model ID (full mode only) |
| `AI_REVIEW_MAX_DIFF_LINES` | `5000` | Skip review when diff exceeds this many lines |
| `AI_REVIEW_MAX_INLINE` | `25` | Max inline comments per run. The summary body holds the excess |
| `AI_REVIEW_MAX_TOKENS_PER_AGENT` | `32768` | Output token budget per LLM agent call |
| `AI_REVIEW_ENABLE_SUGGESTIONS` | `true` | Enable "Apply suggestion" buttons on inline comments |
| `AI_REVIEW_PARALLEL` | `true` | Parallel tiered fan-out. Set to `false` if you hit provider rate limits |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | `true` | Strip upstream base-branch merges from the diff before review |
| `AI_REVIEW_FAIL_ON_FINDINGS` | `true` | Exit with code 2 when the review outcome is `REQUEST_CHANGES` or `COMMENT` (incomplete or unknown risk). This fails the workflow step. **Note**: The `fail-on-findings` input in `action.yml` defaults to `'false'`. The workflow example above sets it to `true` to match the fail-closed default of the container-action starter templates. If you omit it from your own workflow, CI stays green even on a `REQUEST_CHANGES` outcome. Set to `false` to always exit 0. |

This table lists the inputs most consuming repositories need. `action.yml` has many more (SARIF ingestion, analyzer and agent allowlists and denylists, context enrichment, cost ceiling, judge pass, token-usage display, `approval-ceiling`, and others). For the full reference, see [Configuration → Repository variables](configuration#repository-variables).

## Runtime dependencies

The root composite action installs `shellcheck` automatically if the runner does not have it. To get findings from the other static analyzers, install them in the consuming workflow. If a binary is absent, the action writes a WARNING to stderr and continues without those findings.

| Analyzer | Language/files | Install |
|----------|---------------|---------|
| semgrep | Any source files | `pip install semgrep` |
| trufflehog | Secret scanning | `curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh \| sh -s -- -b /usr/local/bin` |
| ruff | Python | `pip install ruff` |
| golangci-lint | Go | `curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh \| sh -s -- -b /usr/local/bin` |
| hadolint | Dockerfiles | Download from [GitHub releases](https://github.com/hadolint/hadolint/releases) |
| checkov | IaC (Terraform, K8s, CloudFormation) | `pip install checkov` |
| phpcs | PHP | `composer global require squizlabs/php_codesniffer` |
| phpstan | PHP | `composer global require phpstan/phpstan` |
| kube-linter | Kubernetes manifests | Download from [GitHub releases](https://github.com/stackrox/kube-linter/releases) |
| tflint | Terraform | Download from [GitHub releases](https://github.com/terraform-linters/tflint/releases) |
| eslint | JS/TS | Needs an `eslint` binary on `PATH`. It does nothing if no config is present |

> For pinned, SHA-verified installs, use the container action. It ships the analyzer binaries (except eslint) at fixed versions, and you do not need to set up the workflow.
