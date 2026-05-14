---
layout: default
title: Getting Started
nav_order: 1
has_children: true
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
      - uses: tag1consulting/ai-pr-review/container-action@main
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That's it — reviews start firing on the next PR. Want slash commands? (`/ai-pr-review rescan`, `review-full`, etc.) — see [Slash commands](slash-commands) for the additional workflow file.

## Supported VCS providers

The same container image drives PR/MR reviews on GitHub, Bitbucket Cloud,
and GitLab. Select the provider via the `VCS_PROVIDER` env var (default: `github`).

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval | Standalone |
|----------|---------------|---------|--------|-------------|----------|------------|
| GitHub | `github` (default) | Yes | Yes | Yes | Yes | Yes |
| Bitbucket Cloud | `bitbucket` | Yes (findings inside) | No | No | No | No |
| GitLab | `gitlab` | Yes | Yes | Yes | Yes | Yes |

See [Bitbucket setup](bitbucket-setup) for Bitbucket Pipelines setup and
[GitLab setup](gitlab-setup) for GitLab CI/CD setup (token scopes, CI
variables, starter pipeline, caveats). The remainder of this page applies
to the GitHub path.

## Requirements

**The container action is the recommended way to run ai-pr-review.** It pulls a public multi-arch image from GHCR (linux/amd64 and linux/arm64) — no additional authentication or toolchain setup required. Most analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint) ship pre-installed at pinned versions. ESLint is not bundled (it runs from the consumer's `node_modules` / `npx` via the project's own config); the review proceeds without ESLint findings if no JS toolchain is present.

If you prefer to run without Docker (e.g., on self-hosted runners without container support), the [direct action reference](installation-direct-action) and [git submodule](installation-submodule) methods work as standard GitHub Actions composite actions. These require:

- **Bash 4+**, **curl**, **jq**, **git**, **gh** — all pre-installed on standard GitHub-hosted runners
- **shellcheck** — installed automatically by the action if not already present
- Static analyzer binaries installed separately if desired (see [runtime dependencies](installation-direct-action#runtime-dependencies))

Both methods require:

- A GitHub token with `pull-requests: write` permission (the default `GITHUB_TOKEN` works for most repos)
- An API key for one of the [supported LLM providers](configuration#supported-llm-providers)

## Installation

The container action is the recommended installation method — it ships most analyzer binaries (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, tflint) pre-installed at pinned, verified versions. The image supports linux/amd64 and linux/arm64 natively. No toolchain setup on your runner.

### Full setup

The example workflow at [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) uses the container action:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  env:
    FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
  with:
    image-tag: 'latest'            # or pin to a release tag, e.g. '0.7.0'
    provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
    api-key: ${{ secrets.AI_REVIEW_API_KEY }}
    base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
    model-standard: ${{ vars.AI_REVIEW_MODEL_STANDARD || '' }}
    model-premium: ${{ vars.AI_REVIEW_MODEL_PREMIUM || '' }}
    review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || 'quick' }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    max-diff-lines: ${{ vars.AI_REVIEW_MAX_DIFF_LINES || '5000' }}
    max-inline: ${{ vars.AI_REVIEW_MAX_INLINE || '25' }}
    max-tokens-per-agent: ${{ vars.AI_REVIEW_MAX_TOKENS_PER_AGENT || '8192' }}
    enable-suggestions: ${{ vars.AI_REVIEW_ENABLE_SUGGESTIONS || 'true' }}
    parallel: ${{ vars.AI_REVIEW_PARALLEL || 'true' }}
    engine: ${{ vars.AI_PR_REVIEW_ENGINE || 'bash' }}
    ignore-merge-commits: ${{ vars.AI_REVIEW_IGNORE_MERGE_COMMITS || 'false' }}
```

See [`examples/README.md`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/README.md) for a complete setup walkthrough including slash commands and provider configuration.

**Secrets and variables** — configure in the consuming repository's settings (Settings → Secrets and variables → Actions):

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `AI_REVIEW_API_KEY` | Secret | Yes | API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | Variable | No | Provider name (default: `anthropic`) |
| `AI_REVIEW_BASE_URL` | Variable | No | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Variable | No | Override the standard model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Variable | No | Override the premium model ID (full mode only) |
| `AI_REVIEW_MAX_DIFF_LINES` | Variable | No | Skip review when diff exceeds this many lines (default: `5000`) |
| `AI_REVIEW_MAX_INLINE` | Variable | No | Max inline comments per run; excess in summary (default: `25`) |
| `AI_REVIEW_MAX_TOKENS_PER_AGENT` | Variable | No | Output token budget per LLM agent (default: `8192`) |
| `AI_REVIEW_ENABLE_SUGGESTIONS` | Variable | No | Enable "Apply suggestion" buttons (default: `true`) |
| `AI_REVIEW_PARALLEL` | Variable | No | Parallel tiered fan-out; set `false` for sequential (default: `true`) |
| `AI_PR_REVIEW_ENGINE` | Variable | No | Compute engine: `bash` or `python` (default: `bash`) |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | Variable | No | Strip upstream base-branch merges from diff (default: `false`) |

See [Configuration → Repository variables](configuration#repository-variables) for the full reference.

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

See [Local development](local-development) for the full reference including provider-specific examples, local clone mounting, git worktree support, and version pinning.

## Other installation methods

- **[Direct action reference](installation-direct-action)** — uses the root composite action directly, without Docker. Installs shellcheck automatically; does not install semgrep, trufflehog, ruff, or golangci-lint.
- **[Git submodule](installation-submodule)** — explicit, auditable version pinning; commits the exact action source into your repository. Uses a 3-job pattern to isolate the PAT used for submodule checkout.
- **[Slash commands](slash-commands)** — add a comment-trigger workflow to enable `/ai-pr-review` commands on PRs.
- **[Bitbucket setup](bitbucket-setup)** — Bitbucket Cloud Pipelines setup guide.
- **[GitLab setup](gitlab-setup)** — GitLab CI/CD setup guide.
- **[Local development](local-development)** — run reviews locally using Docker without a CI runner.
