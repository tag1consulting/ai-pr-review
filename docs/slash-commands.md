---
layout: default
title: Slash Commands
parent: Getting Started
nav_order: 99
---

# Slash commands

> **GitHub-only.** Slash commands rely on GitHub Actions' `issue_comment`
> and `pull_request_review_comment` event triggers, which have no native
> equivalent in Bitbucket Pipelines or GitLab CI. For GitLab workarounds
> (manual pipeline triggers, CI variables), see
> [GitLab setup — slash command alternatives](gitlab-setup#slash-command-alternatives).

AI PR Review supports commands posted as PR comments. Most commands are processed by a workflow that reacts to `issue_comment` events; the `dismiss` command listens on `pull_request_review_comment` events since it operates on inline review threads.

## Quick start

### 1. Copy the starter workflow

```bash
curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/comment-triggers.yml \
  -o .github/workflows/ai-pr-review-commands.yml
```

This is a thin wrapper (~70 lines) that delegates to a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) hosted in the ai-pr-review repository. All command-parsing, review-dispatch, and dismiss/thread-resolution logic lives upstream — you don't need to maintain it.

### 2. Add a `GH_TOKEN` secret {#pat-requirement}

The starter template passes `secrets.GH_TOKEN` as the GitHub token. This **must** be a Personal Access Token (PAT) or GitHub App token — the built-in `GITHUB_TOKEN` does not work for the `dismiss` command.

**Why:** GitHub restricts the `GITHUB_TOKEN` in `pull_request_review_comment`-triggered workflows from calling the `resolveReviewThread` GraphQL mutation. The token technically has `pull-requests: write` permission, but GitHub's integration security model blocks this specific mutation unless the token is a PAT or App token.

**Create a PAT:**
- Classic PAT: go to Settings → Developer settings → Personal access tokens → Tokens (classic). Grant the `repo` scope.
- Fine-grained PAT: grant **Read and write** access to **Pull requests** and **Read** access to **Metadata** on the target repository.

Then add it as a repository secret named `GH_TOKEN` (Settings → Secrets and variables → Actions → New repository secret).

> **Note:** The `rescan`, `review-full`, `skip`, and `help` commands work with `GITHUB_TOKEN`. Only `dismiss` requires a PAT. If you don't use the `dismiss` command, you can pass `github.token` instead — but the dismiss command will fail.

### 3. Verify your API key secret

The starter template references `secrets.ANTHROPIC_API_KEY`. If you use a different provider, update the `api-key` line and optionally uncomment the `provider` input.

### 4. Merge to your default branch

> **This step is required before commands will work.** GitHub runs
> `issue_comment` and `pull_request_review_comment` workflows from the
> **default branch** only. If you add slash commands in the same PR as
> the main review workflow, the review will start working immediately
> (it uses `pull_request` events), but slash commands won't respond
> until that PR merges.

Commit and merge the workflow file. Once it lands on your default branch, post `/ai-pr-review help` in any PR to verify.

> **Tip:** If slash commands were already working before you added the `GH_TOKEN` secret, the `dismiss` command was silently failing. Post `/ai-pr-review help` to confirm the workflow runs after the merge.

## Commands

### `/ai-pr-review rescan`

Forces a full-diff re-review of the PR, bypassing the SHA watermark. Use this when you want a fresh review after a series of small fixup commits.

### `/ai-pr-review review-full`

Triggers a full-mode review using all agents, including architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, and adversarial-general. This takes longer and costs more than the default quick mode.

### `/ai-pr-review skip`

Adds the `skip-ai-review` label to the PR, suppressing the next automated review trigger. Remove the label manually to re-enable automatic reviews.

### `/ai-pr-review dismiss`

Marks a specific AI review finding as a false positive. **Must be posted as a reply to an inline review comment from the bot** — it will not work as a top-level PR comment.

When invoked:
1. Validates that the parent comment was posted by `github-actions[bot]`
2. Resolves the review thread containing that finding
3. Checks whether any unresolved threads remain on the same review
4. If all threads are resolved, dismisses the `CHANGES_REQUESTED` review with an attribution message

This allows selective dismissal — if a review has three findings and only one is a false positive, dismissing that one leaves the `CHANGES_REQUESTED` state in place until the remaining threads are also resolved (either by pushing a fix or dismissing them individually).

### `/ai-pr-review help`

Posts the command list as a reply comment.

## Access control

Commands can only be triggered by users with `OWNER`, `MEMBER`, or `COLLABORATOR` association on the repository. This is enforced via an `author_association` guard on the job's `if:` condition in the consumer workflow. GitHub does **not** enforce this automatically — without the guard, any authenticated user who can comment on a PR could trigger reviews.

## Feedback via emoji reactions

The workflow uses emoji reactions on the triggering comment to indicate status:

| Reaction | Meaning |
|---|---|
| 👀 | Command recognized, processing |
| 🚀 | Review started (rescan/review-full only) |
| 👍 | Command completed successfully |
| 😕 | Command failed or not applicable (e.g. dismiss on a non-bot comment) |

## Default-branch dispatch behavior

The comment-trigger workflow runs from the **default branch** of your repository, not the PR branch. This is a GitHub Actions platform behavior.

**What this means in practice:**
- Changes to the comment-trigger workflow only take effect after they are merged to your default branch.
- A PR that modifies `ai-pr-review-commands.yml` will not use its own updated version of the workflow while that PR is open — it uses the version already on the default branch.

## Customizing

The starter template exposes commented-out inputs for common customizations:

```yaml
# provider: 'anthropic'        # LLM provider
# base-url: ''                  # For openai-compatible/bedrock-proxy
# image-tag: 'latest'           # Pin to a specific container version
# review-mode-default: 'quick'  # Default mode for rescan command
```

Uncomment and modify as needed. The complete list of inputs is documented in the reusable workflow file (`.github/workflows/slash-commands.yml` in this repository).

## Architecture: reusable workflow

The slash command system is implemented as a GitHub Actions [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows):

```
Consumer repo                          ai-pr-review repo
┌─────────────────────┐                ┌────────────────────────────────┐
│ ai-pr-review-       │  workflow_call │ .github/workflows/             │
│   commands.yml      │ ──────────────>│   slash-commands.yml           │
│ (~70 lines)         │   forwards     │ (handle-command + dismiss jobs)│
│                     │   event data   │                                │
│ • on: issue_comment │   + secrets    │ • command parsing              │
│ • on: pr_review_    │                │ • help / skip / rescan /       │
│     comment         │                │   review-full dispatch         │
└─────────────────────┘                │ • dismiss: GraphQL thread      │
                                       │   resolution + review dismiss  │
                                       └────────────────────────────────┘
```

**Benefits:**
- Consumers copy ~70 lines instead of ~455
- Bug fixes and new commands ship upstream — consumers get them automatically on their next run
- The dismiss job's complex GraphQL logic never needs to be understood or maintained by consumers
- Review action invocation stays in sync — no risk of consumers' rescan inputs drifting from their main review workflow

## Extending the command surface

To add custom commands that only apply to your repository, you have two options:

1. **Add a separate job** in your consumer workflow that handles your custom commands before or after calling the reusable workflow.
2. **Open an issue** on the ai-pr-review repo to propose adding the command upstream if it would benefit other consumers.
