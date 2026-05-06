---
layout: default
title: Slash Commands
parent: Getting Started
nav_order: 1
---

# Slash commands

> **GitHub-only.** Slash commands rely on GitHub Actions' `issue_comment`
> and `pull_request_review_comment` event triggers, which have no native
> equivalent in Bitbucket Pipelines or GitLab CI. For GitLab workarounds
> (manual pipeline triggers, CI variables), see
> [GitLab setup — slash command alternatives](gitlab-setup#slash-command-alternatives).

AI PR Review supports commands posted as PR comments. Most commands are processed by a workflow that reacts to `issue_comment` events; the `dismiss` command listens on `pull_request_review_comment` events since it operates on inline review threads.

## Setup

Copy `examples/workflows/comment-triggers.yml` into your repo's `.github/workflows/` directory and merge it to your default branch. See [examples/README.md](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/README.md) for prerequisites.

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

Commands can only be triggered by users with `OWNER`, `MEMBER`, or `COLLABORATOR` association on the repository. This is enforced via an `author_association` guard on the job's `if:` condition in `comment-triggers.yml`. GitHub does **not** enforce this automatically — without the guard, any authenticated user who can comment on a PR could trigger reviews.

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
- A PR that modifies `comment-triggers.yml` will not use its own updated version of the workflow while that PR is open — it uses the version already on the default branch.

## Extending the command surface

To add custom commands, edit your copy of `comment-triggers.yml`. The parsing block is a simple `case` statement — add new entries there and corresponding steps below. The action itself only needs different inputs (`force-full-diff`, `review-mode`, labels); no changes to the action scripts are required.
