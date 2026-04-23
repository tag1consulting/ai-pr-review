# Starter workflows

Copy these files into your repository's `.github/workflows/` directory to enable AI PR reviews.

## Files

| File | Purpose |
|---|---|
| `workflows/pr-review.yml` | Automatic review on PR open/sync |
| `workflows/comment-triggers.yml` | Slash command support (`/ai-pr-review rescan`, etc.) |

Both workflows use the container-action variant, which pulls a pinned image from GHCR with all analyzer binaries pre-installed.

## Setup

### 1. Create a GHCR token

Create a GitHub Personal Access Token with `read:packages` scope at **Settings → Developer settings → Personal access tokens**.

Add it as a repository secret named `GHCR_TOKEN` in your repo (**Settings → Secrets and variables → Actions**).

### 2. Add your LLM API key

Add your provider API key as a repository secret. The examples use `ANTHROPIC_API_KEY`. For other providers, substitute the appropriate secret name and update `api-key` + `provider` in the workflow.

### 3. Copy the workflow files

Download the files directly from GitHub into your repo:

```bash
mkdir -p .github/workflows

curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/pr-review.yml \
  -o .github/workflows/ai-pr-review.yml

curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/comment-triggers.yml \
  -o .github/workflows/ai-pr-review-commands.yml
```

Or, if you have the repo cloned locally:

```bash
mkdir -p .github/workflows
cp /path/to/ai-pr-review/examples/workflows/pr-review.yml       .github/workflows/ai-pr-review.yml
cp /path/to/ai-pr-review/examples/workflows/comment-triggers.yml .github/workflows/ai-pr-review-commands.yml
```

Commit and push. The review runs automatically on the next opened or updated PR.

## Slash commands

Once `comment-triggers.yml` is merged to your default branch, post these commands in any PR comment:

| Command | Effect |
|---|---|
| `/ai-pr-review rescan` | Force full-diff re-review of the PR |
| `/ai-pr-review review-full` | Run all agents (full mode) |
| `/ai-pr-review skip` | Add `skip-ai-review` label to suppress the next review |
| `/ai-pr-review help` | Post the command list as a reply |

Only users with `OWNER`, `MEMBER`, or `COLLABORATOR` association can trigger commands. This is enforced via an `author_association` guard in the workflow — GitHub does **not** do this automatically.

See [docs/slash-commands.md](../docs/slash-commands.md) for full details.

## Important: issue_comment dispatch behavior

The `comment-triggers.yml` workflow runs from the **default branch** of your repository, not from the PR branch. This means:

- Changes to the comment-trigger workflow only take effect **after they are merged** to your default branch.
- If a PR modifies `comment-triggers.yml`, those changes won't be active until the PR is merged.

This is a GitHub Actions platform behavior, not a limitation of this action.

## Using a pinned image version

For reproducible builds, pin the image tag instead of using `latest`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  with:
    image-tag: '0.1.0'   # pin to a specific release
    ...
```

Available tags: `latest`, `v<major>` (e.g. `v2`), `v<major.minor.patch>`.

## Using the non-container action

If you don't want to manage GHCR authentication, use the root composite action instead. It installs shellcheck on the Actions runner but does not install the other analyzer binaries:

```yaml
- uses: tag1consulting/ai-pr-review@main
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ github.token }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
```
