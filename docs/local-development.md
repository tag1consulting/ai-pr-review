---
layout: default
title: Local Development
parent: Configuration
nav_order: 6
---

# Local Development

Run ai-pr-review locally against any open PR with the container image. You do not need a GitHub Actions runner. The image supports linux/amd64 and linux/arm64 natively.

## Prerequisites

- [Docker](https://docs.docker.com/get-started/get-docker/) installed and running
- [GitHub CLI](https://cli.github.com/) configured locally
- A GitHub token (`gh auth token` or a classic PAT with `repo` scope)
- An API key for one of the [supported LLM providers](configuration#supported-llm-providers)

## Quick start: review a PR

The container computes the diff with `git` against a checkout mounted at `/workspace`. You need a local clone of the target repository with the PR branch checked out and `origin/<base-ref>` fetched. The container does **not** clone the repository for you.

### Prepare a local checkout

```bash
git clone https://github.com/owner/repo.git
cd repo
gh pr checkout 42        # check out the PR branch
git fetch origin main    # make sure origin/main is present locally
```

### Resolving `HEAD_SHA`

Every example below uses `HEAD_SHA`, the commit SHA at the tip of the PR branch. Get it from your local checkout:

```bash
HEAD_SHA=$(git rev-parse HEAD)
```

You can also run `gh pr view 42 --repo owner/repo --json headRefOid --jq .headRefOid`, or copy the SHA from the "Commits" tab of the PR in the GitHub UI.

### Anthropic

```bash
HEAD_SHA=$(git rev-parse HEAD)

docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA="$HEAD_SHA" \
  -v "$(pwd):/workspace" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

### OpenAI

```bash
docker run --rm \
  -e AI_PROVIDER=openai \
  -e OPENAI_API_KEY=sk-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA="$HEAD_SHA" \
  -v "$(pwd):/workspace" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

### Google (Gemini)

```bash
docker run --rm \
  -e AI_PROVIDER=google \
  -e GOOGLE_API_KEY=AIza... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA="$HEAD_SHA" \
  -v "$(pwd):/workspace" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

### Bedrock proxy (or any OpenAI-compatible endpoint)

```bash
docker run --rm \
  -e AI_PROVIDER=bedrock-proxy \
  -e BEDROCK_API_KEY=sk-... \
  -e BEDROCK_API_URL=https://your-proxy.example.com/bedrock \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA="$HEAD_SHA" \
  -v "$(pwd):/workspace" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

For a generic OpenAI-compatible endpoint, set `AI_PROVIDER=openai-compatible` and use `OPENAI_API_KEY` in place of `BEDROCK_API_KEY`. Also set `OPENAI_BASE_URL` to the endpoint.

### Troubleshooting: `origin/<base-ref> is not reachable`

If the diff cannot be computed, the run stops with an error that starts like this:

```
git diff against 'origin/main...<sha>' failed (exit N): <stderr>
```

The container does not run `git fetch`. It can use only the refs that are in the mounted checkout. Common causes:

- The run has no `-v "$(pwd):/workspace"` mount. Then `/workspace` is empty and has no refs.
- The local clone does not have `origin/<base-ref>`. Run `git fetch origin <base-ref>` on your machine before you run the container again. If the clone is shallow, add `--depth=50`.
- The checkout has no `origin` remote. Check with `git remote -v`.

## Dry run (no posting)

Set `AI_DRY_RUN=true` to build the review without posting anything to GitHub. The run reads the diff, selects the agents, and prints a short status (the diff line count, the agent count, and the summary length). It does not print the findings. Use it to check your setup before you post:

```bash
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA="$HEAD_SHA" \
  -e AI_DRY_RUN=true \
  -v "$(pwd):/workspace" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

## Git worktrees

If your checkout is a **git worktree** (the `.git` entry is a pointer file, not a directory), you must also mount the `.git` directory of the parent repository. This lets the container resolve refs:

```bash
PARENT_GIT=$(git rev-parse --git-common-dir)

docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=$(git rev-parse HEAD) \
  -e AI_DRY_RUN=true \
  -v "$(pwd):/workspace" \
  -v "$PARENT_GIT:$PARENT_GIT" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

## Forcing a full re-review

The action tracks the last-reviewed commit SHA, so it does not review unchanged code again. To force a full-diff review (for example, after a series of small fixup commits, or after someone deleted the previous review summary), add this option:

```bash
  -e FORCE_FULL_DIFF=true \
```

## Using full mode

By default the container runs in `quick` mode (code-reviewer, silent-failure-hunter, and pr-summarizer). To run all agents, add this option:

```bash
  -e AI_REVIEW_MODE=full \
```

## All environment variables

| Variable | Required | Description |
|---|---|---|
| `AI_PROVIDER` | No (default: `anthropic`) | `anthropic` \| `openai` \| `openai-compatible` \| `google` \| `bedrock-proxy` |
| `ANTHROPIC_API_KEY` | If provider=anthropic | Anthropic API key |
| `OPENAI_API_KEY` | If provider=openai or openai-compatible | OpenAI or compatible API key |
| `GOOGLE_API_KEY` | If provider=google | Google AI API key |
| `BEDROCK_API_KEY` | If provider=bedrock-proxy | Bedrock proxy API key |
| `BEDROCK_API_URL` | If provider=bedrock-proxy | Base URL for the bedrock proxy |
| `GH_TOKEN` | Yes | GitHub token with `repo` scope (use `gh auth token`) |
| `GITHUB_REPOSITORY` | Yes | `owner/repo` |
| `PR_NUMBER` | Yes | Pull request number |
| `BASE_REF` | Yes | Base branch name (e.g. `main`) |
| `HEAD_SHA` | Yes | Head commit SHA (the tip of the PR branch) |
| `AI_DRY_RUN` | No | `true`: build the review and print a short status, do not post to GitHub |
| `AI_REVIEW_MODE` | No | `quick` (default) or `full` |
| `FORCE_FULL_DIFF` | No | `true`: bypass the SHA watermark and review the full PR diff |
| `AI_PARALLEL` | No | `true` (default). Set to `false` to disable the tiered parallel fan-out if the rate limits of your LLM provider cannot sustain it. |
| `AI_CONFIDENCE_THRESHOLD` | No | Minimum confidence, 0–100 (default: 75) |
| `AI_MAX_INLINE` | No | Max inline comments per run (default: 25) |
| `AI_MAX_TOKENS_PER_AGENT` | No | Max tokens per agent call (default: 32768). `AI_MAX_TOKENS_<AGENT>` (for example `AI_MAX_TOKENS_CODE_REVIEWER=8000`) overrides this for one named agent. For the full list, see [Configuration](configuration#per-agent-max-tokens-overrides-env-var-only). |
| `AI_ENABLE_SUGGESTIONS` | No | `true` (default). Enable "Apply suggestion" buttons on inline comments (GitHub and GitLab. Bitbucket ignores it). |
| `LLM_PROMPT_CACHING` | No | `auto` (default). Enable Anthropic/Bedrock prompt caching. `true` forces it on. `false` forces it off. |
| `AI_CACHE_PRIMING` | No | Deprecated and ignored (#824 audit of #807). The cache-priming serialization was deleted as dead code. The action accepts it as a no-op with a deprecation warning. It will reject it starting in v3.0.0. |
| `AI_TEMPERATURE` | No | Sampling temperature for LLM calls (default: 0.3, clamped to [0, 2]). The action does not send it to models that reject or discourage a non-default value (currently Claude Opus 4.7, 4.8, 5 and 5.5, Claude Sonnet 5 and 5.5, Claude Haiku 5.5, OpenAI `o1`/`o3`/`o4`, `gpt-5`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`, and Gemini 3), where the provider's default applies. `gpt-5.4` and `gpt-5.4-mini` still receive it. The exact list is `resolve_temperature()` in `ai_pr_review/llm/_config.py`. |

## Pinning a version

For reproducible runs, replace `:latest` with a specific version tag:

```bash
ghcr.io/tag1consulting/ai-pr-review:2.24.1
# or pin to a major version:
ghcr.io/tag1consulting/ai-pr-review:2
```

Available tags: `latest`, `<major>` (e.g. `2`), `<major.minor>` (e.g. `2.23`), `<major.minor.patch>` (e.g. `2.24.1`).

## Building the image locally

To test changes to the Dockerfile or the code without publishing, build the image locally:

```bash
git clone git@github.com:tag1consulting/ai-pr-review.git
cd ai-pr-review
docker build -t ai-pr-review:dev .
```

Then use `ai-pr-review:dev` in place of the GHCR image in any of the run commands above.
