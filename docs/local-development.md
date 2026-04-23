# Local Development

Run ai-pr-review locally against any open PR using the container image — no GitHub Actions runner needed.

## Prerequisites

- [Docker](https://docs.docker.com/get-started/get-docker/) installed and running
- A GitHub token (`gh auth token` or a classic PAT with `repo` scope)
- An API key for one of the [supported LLM providers](../README.md#supported-llm-providers)
- A GitHub PAT with `read:packages` scope to pull the private image

## One-time setup

Authenticate to GHCR with a PAT that has `read:packages` scope:

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME -p YOUR_GHCR_PAT
```

This is stored in `~/.docker/config.json` and persists across sessions.

## Quick start: review a PR

The container fetches the diff directly from GitHub — you don't need a local clone.

### Anthropic

```bash
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=abc1234 \
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
  -e HEAD_SHA=abc1234 \
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
  -e HEAD_SHA=abc1234 \
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
  -e HEAD_SHA=abc1234 \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

For a generic OpenAI-compatible endpoint, use `AI_PROVIDER=openai-compatible` and `OPENAI_API_KEY` instead of `BEDROCK_API_KEY`.

## Finding the HEAD_SHA

```bash
# From the GitHub UI: copy the commit SHA from the PR's "Commits" tab.

# Or with the gh CLI:
gh pr view 42 --repo owner/repo --json headRefOid --jq .headRefOid

# Or from a local clone on the PR branch:
git rev-parse HEAD
```

## Dry run (no posting)

Set `AI_DRY_RUN=true` to print findings to stdout without posting anything to GitHub. Good for testing before you're ready to post, or for iterating on suppression rules:

```bash
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=$(gh auth token) \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=abc1234 \
  -e AI_DRY_RUN=true \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

## With a local repo checkout

Mounting your local clone avoids a remote git fetch and lets you test changes to prompts or scripts before pushing:

```bash
# Run from the root of your local clone (on the PR branch)
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
  ghcr.io/tag1consulting/ai-pr-review:latest
```

The container needs to reach the base branch ref (`origin/main`) to compute the diff. If it can't — for example, if you cloned with `--depth=1` — it will fall back to fetching over HTTPS using `GH_TOKEN` automatically.

### Git worktrees

If your checkout is a **git worktree** (the `.git` entry is a pointer file rather than a directory), you must also mount the parent repository's `.git` directory so the container can resolve refs:

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

The action tracks the last-reviewed commit SHA to avoid re-reviewing unchanged code. If you want to force a full-diff review regardless (for example, after a series of small fixup commits, or when the previous review summary was deleted):

```bash
  -e FORCE_FULL_DIFF=true \
```

## Using full mode

By default the container runs in `quick` mode (code-reviewer + silent-failure-hunter). To run all agents:

```bash
  -e AI_REVIEW_MODE=full \
```

## All environment variables

| Variable | Required | Description |
|---|---|---|
| `AI_PROVIDER` | Yes | `anthropic` \| `openai` \| `openai-compatible` \| `google` \| `bedrock-proxy` |
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
| `AI_DRY_RUN` | No | `true` — print findings to stdout, do not post to GitHub |
| `AI_REVIEW_MODE` | No | `quick` (default) or `full` |
| `FORCE_FULL_DIFF` | No | `true` — bypass the SHA watermark; review the full PR diff |
| `AI_PARALLEL` | No | `true` (default) or `false` — disable to reduce concurrent API calls |
| `AI_CONFIDENCE_THRESHOLD` | No | Minimum confidence 0–100 (default: 75) |
| `AI_MAX_INLINE` | No | Max inline comments per run (default: 25) |
| `AI_MAX_TOKENS_PER_AGENT` | No | Max tokens per agent call (default: 8192) |

## Pinning a version

Replace `:latest` with a specific version tag for reproducible runs:

```bash
ghcr.io/tag1consulting/ai-pr-review:v2.0.0
# or pin to a major version:
ghcr.io/tag1consulting/ai-pr-review:v2
```

Available tags: `latest`, `v<major>` (e.g. `v2`), `v<major.minor.patch>`.

## Building the image locally

To test changes to the Dockerfile or scripts without publishing:

```bash
git clone git@github.com:tag1consulting/ai-pr-review.git
cd ai-pr-review
docker build -t ai-pr-review:dev .
```

Then substitute `ai-pr-review:dev` for the GHCR image in any of the run commands above.
