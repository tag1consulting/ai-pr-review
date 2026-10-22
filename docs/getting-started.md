---
layout: default
title: Getting Started
nav_order: 1
render_with_liquid: false
---

# Getting Started

## Quickstart

Get AI reviews on your PRs in two steps:

**1. Add your LLM API key** as a repository secret named `ANTHROPIC_API_KEY` (or the equivalent for your [provider](configuration#supported-llm-providers)).

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

Reviews start on the next PR. To use slash commands (`/ai-pr-review rescan`, `review-full`, `dismiss`, and others), use the full template from [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml). That template sets up automatic review and slash commands in one file. See [Slash commands](slash-commands) for details.

## Supported VCS providers

The same container image reviews PRs and MRs on GitHub, Bitbucket Cloud, and GitLab. Select the provider with the `VCS_PROVIDER` environment variable (default: `github`).

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval |
|----------|---------------|---------|--------|-------------|----------|
| GitHub | `github` (default) | Yes | Yes | Yes | Yes |
| Bitbucket Cloud | `bitbucket` | Yes | Yes (through Code Insights annotations, not inline comments. See [Bitbucket setup](bitbucket-setup.md)) | No | Yes (real approve/request-changes calls, default on via `AI_BITBUCKET_REVIEW_STATE`) |
| GitLab | `gitlab` | Yes | Yes | Yes | Yes |

For Bitbucket Pipelines, see [Bitbucket setup](bitbucket-setup). For GitLab CI/CD (token scopes, CI variables, starter pipeline, caveats), see [GitLab setup](gitlab-setup). The remainder of this page applies to the GitHub path.

## Requirements

**Use the container action to run ai-pr-review. This is the recommended method.** It pulls a public multi-arch image from GHCR (linux/amd64 and linux/arm64). You need no extra authentication or toolchain setup. Most analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint) come pre-installed at pinned versions. ESLint is not bundled. It runs from the `node_modules` or `npx` of the consuming project, with the project's own config. If no JS toolchain is present, the review continues without ESLint findings.

To run without Docker (for example, on self-hosted runners without container support), use the [direct action reference](installation-direct-action) or the [git submodule](installation-submodule) method. Both work as standard GitHub Actions composite actions. They need:

- **Python 3.11 or later**, **pip**, **git**, and **gh**. Standard GitHub-hosted runners have these pre-installed.
- **shellcheck**: the action installs it automatically if it is not present.
- Static analyzer binaries, which you can install separately if you want them (see [runtime dependencies](installation-direct-action#runtime-dependencies)).

Both methods also need:

- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repositories)
- An API key for one of the [supported LLM providers](configuration#supported-llm-providers)

## Installation

The container action is the recommended installation method. It ships most analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint) pre-installed at pinned, verified versions. The image supports linux/amd64 and linux/arm64 natively. You do not need to set up a toolchain on your runner.

### Full setup

The example workflow at [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) uses the container action. This is an excerpt:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  env:
    FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
  with:
    image-tag: ${{ vars.AI_REVIEW_IMAGE_TAG || 'latest' }}  # or pin to a release tag, for example '2.25.0'
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
    # --- Optional capabilities ---
    context-enrichment: ${{ vars.AI_REVIEW_CONTEXT_ENRICHMENT || 'true' }}
    sarif-paths: ${{ vars.AI_REVIEW_SARIF_PATHS || '' }}
    feedback-loop: ${{ vars.AI_REVIEW_FEEDBACK_LOOP || 'false' }}
```

For a complete setup walkthrough that includes slash commands and provider configuration, see [`examples/README.md`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/README.md).

**Secrets and variables:** Configure these in the settings of the consuming repository (Settings → Secrets and variables → Actions).

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `AI_REVIEW_API_KEY` | Secret | Yes | API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | Variable | No | Provider name (default: `anthropic`) |
| `AI_REVIEW_BASE_URL` | Variable | No | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Variable | No | Override the standard model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Variable | No | Override the premium model ID (full mode only) |
| `AI_REVIEW_MAX_DIFF_LINES` | Variable | No | Skip review when diff exceeds this many lines (default: `5000`) |
| `AI_REVIEW_MAX_INLINE` | Variable | No | Max inline comments per run. The summary holds the excess (default: `25`) |
| `AI_REVIEW_MAX_TOKENS_PER_AGENT` | Variable | No | Output token budget per LLM agent (default: `32768`) |
| `AI_REVIEW_ENABLE_SUGGESTIONS` | Variable | No | Enable "Apply suggestion" buttons (default: `true`) |
| `AI_REVIEW_PARALLEL` | Variable | No | Parallel tiered fan-out. Set to `false` for sequential runs (default: `true`) |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | Variable | No | Strip upstream base-branch merges from diff (default: `true`. Set to `false` to include upstream merges) |
| `AI_REVIEW_IMAGE_TAG` | Variable | No | Container image tag (default: `latest`). Set to `dev` to test pre-release builds, or pin to a release |
| `AI_REVIEW_CONTEXT_ENRICHMENT` | Variable | No | **Context enrichment.** Tree-sitter symbol-context injection (default: `true` in the container action) |
| `AI_REVIEW_SARIF_PATHS` | Variable | No | **SARIF ingestion.** Comma-separated SARIF 2.1.0 paths to merge as findings (default: `''`) |
| `AI_REVIEW_FEEDBACK_LOOP` | Variable | No | **Learning loop.** Enable the learning loop (default: `false`. GitHub and Bitbucket only, see [docs/learning-loop.md](learning-loop.md)) |

See [Configuration → Repository variables](configuration#repository-variables) for the full reference.

**Local development:** Run reviews against any open PR without a CI runner.

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

A dry run does not print the findings. To post findings to the PR, remove `-e AI_DRY_RUN=true`. For other providers, change `AI_PROVIDER` and the matching key variable (`openai`/`OPENAI_API_KEY`, `google`/`GOOGLE_API_KEY`, `bedrock-proxy`/`BEDROCK_API_KEY`+`BEDROCK_API_URL`).

For the full reference, see [Local development](local-development). It covers provider-specific examples, local clone mounting, git worktree support, and version pinning.

## Other installation methods

- **[Direct action reference](installation-direct-action)**: Uses the root composite action directly, without Docker. It installs shellcheck automatically. It does not install semgrep, trufflehog, ruff, or golangci-lint.
- **[Git submodule](installation-submodule)**: Pins an exact, auditable version and commits the action source into your repository. It works with the default token because the repository is public. For a private fork or mirror, use a 3-job pattern to isolate the PAT.
- **[Slash commands](slash-commands)**: Add a comment-trigger workflow to enable `/ai-pr-review` commands on PRs.
- **[Bitbucket setup](bitbucket-setup)**: Bitbucket Cloud Pipelines setup guide.
- **[GitLab setup](gitlab-setup)**: GitLab CI/CD setup guide.
- **[Local development](local-development)**: Run reviews locally with Docker, without a CI runner.
