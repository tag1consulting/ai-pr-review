# Slash commands

AI PR Review supports commands posted as PR comments. Commands are processed by a separate workflow that reacts to `issue_comment` events.

## Setup

Copy `examples/workflows/comment-triggers.yml` into your repo's `.github/workflows/` directory and merge it to your default branch. See [examples/README.md](../examples/README.md) for prerequisites.

## Commands

### `/ai-pr-review rescan`

Forces a full-diff re-review of the PR, bypassing the SHA watermark. Use this when you want a fresh review after a series of small fixup commits.

### `/ai-pr-review review-full`

Triggers a full-mode review using all agents, including architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, and adversarial-general. This takes longer and costs more than the default quick mode.

### `/ai-pr-review skip`

Adds the `skip-ai-review` label to the PR, suppressing the next automated review trigger. Remove the label manually to re-enable automatic reviews.

### `/ai-pr-review help`

Posts the command list as a reply comment.

## Access control

Commands can only be triggered by users with **write access** to the repository. GitHub enforces this automatically — `issue_comment` events from users with only read/triage access do not dispatch the workflow.

## Feedback via emoji reactions

The workflow uses emoji reactions on the triggering comment to indicate status:

| Reaction | Meaning |
|---|---|
| 👀 | Command recognized, processing |
| 🚀 | Review started |
| 👍 | Review completed successfully |
| 😕 | Review failed |

## Default-branch dispatch behavior

The comment-trigger workflow runs from the **default branch** of your repository, not the PR branch. This is a GitHub Actions platform behavior.

**What this means in practice:**
- Changes to the comment-trigger workflow only take effect after they are merged to your default branch.
- A PR that modifies `comment-triggers.yml` will not use its own updated version of the workflow while that PR is open — it uses the version already on the default branch.

## Extending the command surface

To add custom commands, edit your copy of `comment-triggers.yml`. The parsing block is a simple `case` statement — add new entries there and corresponding steps below. The action itself only needs different inputs (`force-full-diff`, `review-mode`, labels); no changes to the action scripts are required.
