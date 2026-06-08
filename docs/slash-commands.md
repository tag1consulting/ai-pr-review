---
layout: default
title: Slash Commands
parent: Getting Started
nav_order: 7
---

# Slash commands

> **GitHub-only.** Slash commands rely on GitHub Actions' `issue_comment`
> and `pull_request_review_comment` event triggers, which have no native
> equivalent in Bitbucket Pipelines or GitLab CI. For GitLab workarounds
> (manual pipeline triggers, CI variables), see
> [GitLab setup — slash command alternatives](gitlab-setup#slash-command-alternatives).

AI PR Review supports commands posted as PR comments. The workflow listens on both `issue_comment` (top-level PR comments) and `pull_request_review_comment` (replies on inline review threads) events.

**Two classes of findings:**
- **Inline findings** are anchored to a specific diff line. They appear as review-thread comments. Use `/ai-pr-review dismiss` (or `false-positive`, `wont-fix`) as a **reply** on the thread to dismiss them.
- **Body-level findings** appear in the `### Findings not attached to specific lines` section of the review body. They have no thread to reply to. Each one is labeled with a stable ID like `**[F1]**`. Use `/ai-pr-review dismiss F1` (or `false-positive F1`, `wont-fix F1`) as a **top-level PR comment** to dismiss them.

## Quick start

### 1. Copy the starter workflow

```bash
curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/comment-triggers.yml \
  -o .github/workflows/ai-pr-review-commands.yml
```

This is a thin wrapper (~70 lines) that delegates to a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) hosted in the ai-pr-review repository. All command-parsing, review-dispatch, and dismiss/thread-resolution logic lives upstream — you don't need to maintain it.

### 2. Add a `GH_TOKEN` secret {#pat-requirement}

The starter template uses **two tokens**:

- **`secrets.GH_TOKEN`** (PAT or GitHub App token) — required only for the `dismiss` command's `resolveReviewThread` GraphQL mutation and review-dismissal REST calls.
- **`secrets.GITHUB_TOKEN`** (the built-in token, auto-available in every repository) — used for all plain comment posts, reactions, reads, label changes, and checkout. These operations post as **`github-actions[bot]`**.

**Why a PAT is still needed for `dismiss`:** GitHub restricts `GITHUB_TOKEN` in `pull_request_review_comment`-triggered workflows from calling the `resolveReviewThread` GraphQL mutation. The token technically has `pull-requests: write` permission, but GitHub's integration security model blocks this specific mutation unless the token is a PAT or App token.

**You do not need to add a `GH_TOKEN` secret** if you only use `rescan`, `review-full`, `skip`, `help`, or the learning-loop commands (`false-positive`, `wont-fix`, `feedback`, `explain`, `revise`). Those all run under `GITHUB_TOKEN`. The `dismiss` command will fail without `GH_TOKEN`.

**Create a PAT for `dismiss` support:**
- Classic PAT: go to Settings → Developer settings → Personal access tokens → Tokens (classic). Grant the `repo` scope.
- Fine-grained PAT: grant **Read and write** access to **Pull requests** and **Read** access to **Metadata** on the target repository.

Then add it as a repository secret named `GH_TOKEN` (Settings → Secrets and variables → Actions → New repository secret).

> **Known edge:** A few confirmation messages inside the `dismiss` command's resolve/dismiss steps still post under the PAT rather than `GITHUB_TOKEN`, because those messages are interleaved with the GraphQL mutation in the same shell step. This affects only the dismiss-outcome confirmation comment, not the lookup/list/learning-loop replies. A future refactor will move those to `GITHUB_TOKEN` as well.

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

Marks a specific AI review finding as a false positive.

#### For inline findings (reply on the review thread)

Post as a **reply to the bot's inline review comment**. When invoked:
1. Validates that the parent comment was posted by `github-actions[bot]`
2. Resolves the review thread containing that finding
3. Checks whether any unresolved threads remain on the same review
4. If all threads are resolved, dismisses the `CHANGES_REQUESTED` review with an attribution message

This allows selective dismissal — if a review has three findings and only one is a false positive, dismissing that one leaves the `CHANGES_REQUESTED` state in place until the remaining threads are also resolved.

#### For body-level findings (top-level PR comment with `F<n>`)

Body-level findings appear in the `### Findings not attached to specific lines` section. Each is labeled with a **stable per-PR ID** like `**[F1]**`, `**[F2]**`, etc. To dismiss one:

```
/ai-pr-review dismiss F1
```

IDs are **PR-wide and stable across review cycles** — if `F1` was assigned to a finding in the first review, it refers to the same finding in every subsequent review. New findings introduced by later reviews get the next unused ID (IDs are never re-used). A gap like `F1, F3` (no `F2`) signals that `F2` was dismissed in a prior cycle.

If you post `/ai-pr-review dismiss` without an ID as a top-level PR comment, the bot replies with the list of active body-level finding IDs and the correct syntax.

When all **inline** review threads are resolved, the `CHANGES_REQUESTED` review is automatically dismissed — the same behavior as the inline path. Remaining body-level findings will be suppressed on the next re-review run once they are recorded in the feedback store.

### `/ai-pr-review false-positive [F<n>] [reason]`

Records the finding as a false positive in the learning loop. The `[reason]` is optional but encouraged — it helps future reviews avoid the same finding in similar contexts.

**For inline findings:** Post as a reply on the AI's inline review-comment thread. The workflow auto-extracts source / file / rule_id from the parent comment and resolves the thread on success.

**For body-level findings:** Post as a top-level PR comment with the finding's stable ID:
```
/ai-pr-review false-positive F2 documented via JSDoc, not the shortcode param block
```

Requires `AI_FEEDBACK_LOOP=true` on the action input and a `GH_TOKEN` with `contents:write` on the feedback branch (default: `ai-pr-review-bot`). The entry is persisted to `.ai-pr-review/learnings.jsonl` on that branch.

### `/ai-pr-review wont-fix [F<n>] [reason]`

Records the finding as intentional / won't-fix. Use this when the finding is valid but the pattern is deliberate in this codebase (e.g. intentional use of MD5 for non-security checksums, intentional exception swallowing in a specific error handler).

**For inline findings:** Reply on the thread (same rules as `false-positive`).

**For body-level findings:** Top-level PR comment with finding ID:
```
/ai-pr-review wont-fix F3 intentional behavior — see design doc
```

Requires the same setup as `false-positive`.

### `/ai-pr-review feedback <text>`

Stores free-form feedback in the learning loop — not tied to a specific finding verdict. Useful for noting that a certain category of finding is too noisy for this repository.

### `/ai-pr-review explain [F<n>]`

Requests a more detailed explanation from the originating agent. Currently stubbed — the command is recognized and acknowledged, but full agent re-invocation is not yet implemented. Posts a canned reply.

**For inline findings:** Post as a reply on the AI's inline review-comment thread.

**For body-level findings:** Post as a top-level PR comment with the finding's stable ID:
```
/ai-pr-review explain F2
```

### `/ai-pr-review revise [F<n>] <hint>`

Requests the originating agent to revise its finding with the provided hint. Currently stubbed — same as `explain`.

**For inline findings:** Post as a reply on the AI's inline review-comment thread.

**For body-level findings:** Post as a top-level PR comment with the finding's stable ID and hint:
```
/ai-pr-review revise F3 focus on the icon card variant specifically
```

### `/ai-pr-review help`

Posts the command list as a reply comment.

## Learning loop setup

The learning loop (`AI_FEEDBACK_LOOP=true`) stores feedback in a JSONL file on a dedicated git branch (`ai-pr-review-bot` by default). To enable it:

1. Set `feedback-loop: 'true'` in the action input.
2. Ensure `GH_TOKEN` has `contents:write` on the feedback branch. The branch is created automatically on first write.
3. Optionally set `feedback-branch` to a custom branch name.

See [Learning loop](learning-loop.md) for the full architecture and retention policy.

> **GitLab / Bitbucket:** The learning loop is GitHub-only in this release. On other providers, feedback commands are silently no-ops.

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
