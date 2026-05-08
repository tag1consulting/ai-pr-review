---
layout: default
title: Direct Action Reference
parent: Getting Started
nav_order: 6
render_with_liquid: false
---

# Installation: Direct action reference

Use this installation method if you prefer to run without Docker. The root
composite action installs shellcheck on the runner automatically but does
**not** install other static analyzer binaries (semgrep, trufflehog, ruff,
golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint, or
eslint). See [Static analyzers](static-analyzers) for details. Each
analyzer is a graceful no-op when its binary is absent.

For most new installs, prefer the
[container action](getting-started) — it ships all analyzer binaries
pre-installed at pinned versions.

## Prerequisites

In this repo's settings, go to **Settings → Actions → General → Access** and
set it to **"Accessible from repositories in the 'tag1consulting'
organization"**. This allows other repos in the org to use it as an action.

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

Pin to a specific version by replacing `@main` with a tag or commit SHA
(e.g. `@v0.2.0` or `@d613707`).

## 2. Configure secrets and variables

In the **consuming** repository's settings:

**Secrets:**
- `AI_REVIEW_API_KEY` — API key for your chosen LLM provider

**Variables** (optional):
- `AI_REVIEW_PROVIDER` — Provider name (default: `anthropic`)
- `AI_REVIEW_BASE_URL` — Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`)
- `AI_REVIEW_MODEL_STANDARD` — Override the standard model ID
- `AI_REVIEW_MODEL_PREMIUM` — Override the premium model ID (full mode only)

## Runtime dependencies

The root composite action installs `shellcheck` automatically if it is not
already present on the runner. All other static analyzers must be installed by
the consuming workflow if you want their findings. The action degrades
gracefully if any binary is absent — it emits a WARNING on stderr and continues
without those findings.

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
| eslint | JS/TS | Uses the project's own `node_modules/.bin/eslint` or `npx`; no-op if no config present |

> For pinned, SHA-verified installs use the container action instead — it ships
> all analyzer binaries (except eslint) at fixed versions without any workflow setup.
