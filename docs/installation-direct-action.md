# Installation: Direct action reference

Use this installation method if you cannot or do not want to pull a private
container image from GHCR. The root composite action installs shellcheck on
the runner automatically but does **not** install semgrep, trufflehog, ruff,
or golangci-lint. See [Static analyzers](../README.md#static-analyzers) in the
main README if you need those.

For most new installs, prefer the
[container action](../README.md#installation) — it ships all analyzer binaries
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
already present on the runner. All other static analyzers (`semgrep`,
`trufflehog`, `ruff`, `golangci-lint`) must be installed by the consuming
workflow if you want their findings. The action degrades gracefully if they are
absent — it emits a WARNING on stderr and continues without those findings.

Add a step before the action call to install whichever analyzers you need:

```yaml
- name: Install static analyzers
  run: |
    pip install semgrep ruff
    curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin
    curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/master/install.sh | sh -s -- -b /usr/local/bin
```

> For pinned, SHA-verified installs use the container action instead — it ships
> all analyzer binaries at fixed versions without any workflow setup.
