# Starter workflows and pipelines

Copy these files into your repository to enable AI PR/MR reviews.

## Files

| File | Purpose |
|---|---|
| `workflows/pr-review.yml` | GitHub Actions: automatic review on PR open/sync |
| `workflows/comment-triggers.yml` | GitHub Actions: slash command support — thin wrapper that calls the reusable workflow (`/ai-pr-review rescan`, etc.) |
| `pipelines/bitbucket-pipelines.yml` | Bitbucket Pipelines: automatic review on PR open/update |
| `pipelines/.gitlab-ci.yml` | GitLab CI: automatic review on MR open/update |

GitHub workflows use the container-action variant, which pulls a pinned public image from GHCR with all analyzer binaries pre-installed. Bitbucket and GitLab pipelines use the same image directly.

## Setup

### 1. Add your LLM API key

Add your provider API key as a repository secret. The examples use `ANTHROPIC_API_KEY`. For other providers, substitute the appropriate secret name and update `api-key` + `provider` in the workflow.

### 2. Copy the workflow files

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

## Auto-full for release PRs

The example workflow auto-selects full review mode when the source branch starts with `release/`. Customize the `startsWith()` pattern in the `review-mode` expression for your repo's branch convention. See [README > Auto-detecting release PRs](../README.md#auto-detecting-release-prs) for common patterns.

## Slash commands

The `comment-triggers.yml` starter is a thin wrapper (~55 lines) that calls a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) hosted in this repository. All command parsing, review dispatch, and dismiss logic lives upstream — consumers don't need to maintain it.

Once the file is merged to your default branch, post these commands in any PR comment:

| Command | Effect |
|---|---|
| `/ai-pr-review rescan` | Force full-diff re-review of the PR |
| `/ai-pr-review review-full` | Run all agents (full mode) |
| `/ai-pr-review skip` | Add `skip-ai-review` label to suppress the next review |
| `/ai-pr-review help` | Post the command list as a reply |
| `/ai-pr-review dismiss` | Reply to an inline review comment to mark that thread a false positive (dispatched via `pull_request_review_comment`, not `issue_comment`) |

Only users with `OWNER`, `MEMBER`, or `COLLABORATOR` association can trigger commands. This is enforced via an `author_association` guard in the workflow — GitHub does **not** do this automatically.

See [docs/slash-commands.md](../docs/slash-commands.md) for full details.

## Important: default-branch dispatch

Both `issue_comment` and `pull_request_review_comment` workflows run from the **default branch** of your repository, not from the PR branch. This means:

- Slash commands only work **after `comment-triggers.yml` is merged** to your default branch.
- If you add slash commands in the same PR as the main review workflow, the review will start working immediately (it uses `pull_request` events), but slash commands won't respond until that PR merges.

This is a GitHub Actions platform behavior, not a limitation of this action.

## Using a pinned image version

For reproducible builds, pin the image tag instead of using `latest`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  with:
    image-tag: '0.7.0'   # pin to a specific release
    ...
```

Available tags: `latest`, `<major>` (e.g. `0`), `<major.minor>` (e.g. `0.7`), `<major.minor.patch>` (e.g. `0.7.0`).

## Using the non-container action

To skip Docker entirely, use the root composite action instead. It installs shellcheck on the Actions runner but does not install the other analyzer binaries:

```yaml
- uses: tag1consulting/ai-pr-review@main
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ github.token }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
```
