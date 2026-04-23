# Local Development

Run ai-pr-review locally against any PR using the container image — no GitHub Actions runner needed.

## One-time setup

Authenticate to GHCR with a GitHub Personal Access Token that has `read:packages` scope:

```bash
docker login ghcr.io -u YOUR_GITHUB_USERNAME -p YOUR_PAT
```

This is stored in `~/.docker/config.json` and persists across sessions. You only need to re-run it if your PAT rotates.

## Running against a PR

```bash
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=ghp_... \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=abc1234 \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

The container fetches the diff from GitHub and posts findings back to the PR. The repo does **not** need to be checked out locally.

## Dry run (no posting)

Set `AI_DRY_RUN=true` to print findings to stdout without posting anything to GitHub. Useful for iterating on prompts or suppression rules:

```bash
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=ghp_... \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=abc1234 \
  -e AI_DRY_RUN=true \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

## With a local repo checkout

Mount your workspace to use a local clone. The container still needs to fetch the base branch ref — configure HTTPS auth via `GIT_CONFIG_GLOBAL` or pass `GH_TOKEN` (used automatically by `gh` inside the container):

```bash
docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=ghp_... \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=$(git rev-parse HEAD) \
  -e AI_DRY_RUN=true \
  -v "$(pwd):/workspace" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

### Git worktrees

If your checkout is a **git worktree** (the `.git` entry is a file pointer rather than a directory), you must also mount the parent repository's `.git` directory so the container can resolve refs:

```bash
# Find the parent .git path
PARENT_GIT=$(git rev-parse --git-common-dir)

docker run --rm \
  -e AI_PROVIDER=anthropic \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e GH_TOKEN=ghp_... \
  -e GITHUB_REPOSITORY=owner/repo \
  -e PR_NUMBER=42 \
  -e BASE_REF=main \
  -e HEAD_SHA=$(git rev-parse HEAD) \
  -e AI_DRY_RUN=true \
  -v "$(pwd):/workspace" \
  -v "$PARENT_GIT:$PARENT_GIT" \
  ghcr.io/tag1consulting/ai-pr-review:latest
```

### Bypassing the SHA watermark

If the PR has already been reviewed at the current HEAD, the container will exit with "No new changes since last review." To force a full-diff re-review:

```bash
  -e FORCE_FULL_DIFF=true \
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `AI_PROVIDER` | Yes | `anthropic` \| `openai` \| `openai-compatible` \| `google` \| `bedrock-proxy` |
| `ANTHROPIC_API_KEY` | If provider=anthropic | API key |
| `OPENAI_API_KEY` | If provider=openai/openai-compatible | API key |
| `GOOGLE_API_KEY` | If provider=google | API key |
| `BEDROCK_API_KEY` | If provider=bedrock-proxy | API key |
| `GH_TOKEN` | Yes | GitHub token with `repo` scope |
| `GITHUB_REPOSITORY` | Yes | `owner/repo` |
| `PR_NUMBER` | Yes (PR mode) | Pull request number |
| `BASE_REF` | Yes | Base branch name (e.g. `main`) |
| `HEAD_SHA` | Yes | Head commit SHA |
| `AI_DRY_RUN` | No | Set to `true` to print findings without posting |
| `AI_REVIEW_MODE` | No | `quick` (default) or `full` |
| `FORCE_FULL_DIFF` | No | `true` to bypass SHA watermark |
| `AI_PARALLEL` | No | `true` (default) or `false` |
| `AI_CONFIDENCE_THRESHOLD` | No | Minimum confidence 0–100 (default: 75) |
| `AI_MAX_INLINE` | No | Max inline comments per run (default: 25) |
| `AI_MAX_TOKENS_PER_AGENT` | No | Max tokens per agent call (default: 8192) |

## Pinning a version

Replace `:latest` with a specific version tag for reproducible runs:

```bash
ghcr.io/tag1consulting/ai-pr-review:v2.0.0
```

Available tags: `latest`, `v<major>` (e.g. `v2`), `v<major.minor.patch>`.

## Building the image locally

```bash
git clone git@github.com:tag1consulting/ai-pr-review.git
cd ai-pr-review
docker build -t ai-pr-review:dev .
```

Then substitute `ai-pr-review:dev` for the GHCR image in the run commands above.
