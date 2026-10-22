---
layout: default
title: Slash Commands
nav_order: 4
render_with_liquid: false
---

# Slash commands

> **This page describes the GitHub real-time model.** The commands on this page use the GitHub Actions `issue_comment` and `pull_request_review_comment` event triggers. Bitbucket Pipelines and GitLab CI have no native equivalent. A command therefore takes effect immediately after you post it only on GitHub. For GitLab workarounds (manual pipeline triggers, CI variables), see [GitLab setup: slash command alternatives](gitlab-setup#slash-command-alternatives). Bitbucket has a real, working subset of these commands. The subset has these limits: only `dismiss`, `false-positive`, `wont-fix`, `fixed`, and `feedback` work, only as top-level comments, with no inline-reply form. The next review run applies them, not the comment itself. See [Bitbucket setup: Dismissing findings](bitbucket-setup#dismissing-findings).

AI PR Review supports commands that you post as PR comments. The workflow listens on two events: `issue_comment` (top-level PR comments) and `pull_request_review_comment` (replies on inline review threads).

**Two classes of findings:**
- **Inline findings** are anchored to a specific diff line. They appear as review-thread comments. To dismiss one, post `/ai-pr-review dismiss` (or `false-positive`, `wont-fix`, `fixed`) as a **reply** on the thread.
- **Body-level findings** appear in the `### Findings not attached to specific lines` section of the review body. They have no thread to reply to. Each one has a stable ID, for example `**[F1]**`. To dismiss one, post `/ai-pr-review dismiss F1` (or `false-positive F1`, `wont-fix F1`, `fixed F1`) as a **top-level PR comment**.

**Multiple commands in one top-level comment:** You can post `dismiss`, `false-positive`, `wont-fix`, `fixed`, and `feedback` several times in one top-level PR comment. Put one command on each line. The workflow acts on every line, not only the first:

```
/ai-pr-review dismiss F1
/ai-pr-review wont-fix F2 documented via JSDoc, not the shortcode param block
/ai-pr-review fixed F3 abc1234
```

The bot posts one combined reply that covers every line. This does **not** apply to replies on an inline review-comment thread, because those replies are about the one finding in that thread. It also does not apply to `explain` and `revise` (currently unimplemented stubs). For those commands, the workflow reads only the first line of the comment.

## Quick start

### 1. Use the unified workflow template

The canonical `pr-review.yml` template includes slash commands, so you need no separate file. If you followed the [Getting Started](getting-started) guide and copied `pr-review.yml` to `.github/workflows/ai-pr-review.yml`, the `slash-commands` job in that file already wires them in.

If you have only the minimal quickstart workflow (PR review only), replace it with the full template:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/pr-review.yml \
  -o .github/workflows/ai-pr-review.yml
```

The `slash-commands` job in that template calls a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) in the ai-pr-review repository. The upstream workflow contains all of the command parsing, review dispatch, and dismiss and thread-resolution logic. You do not need to maintain it.

**Existing two-file setup?** If you already have a separate `ai-pr-review-commands.yml` that uses `comment-triggers.yml`, it continues to work unchanged. You do not need to migrate.

### 2. Add a `GH_TOKEN` secret {#pat-requirement}

The starter template uses **two tokens**:

- **`secrets.GH_TOKEN`** (PAT or GitHub App token): The workflow uses it for the `resolveReviewThread` and `unresolveReviewThread` GraphQL mutations. It also uses it in the `authorize` job (see [Access control](#access-control)). Every other write that the `dismiss` command makes runs under `GITHUB_TOKEN`, if you pass `actions-token` (see below). These writes include dismissing a superseded review, the auto-approve review, and replies. The workflow does not route them through the PAT.
- **`secrets.GITHUB_TOKEN`** (the built-in token, available in every repository): The workflow uses it for all plain comment posts, reactions, reads, label changes, and checkout. It also uses it for every `dismiss`-command write except the two GraphQL mutations above (the fix for [#734](https://github.com/tag1consulting/ai-pr-review/issues/734)). These operations post as **`github-actions[bot]`**.

**Why a PAT is necessary to resolve threads:** GitHub does not let `GITHUB_TOKEN` call the `resolveReviewThread` GraphQL mutation in `pull_request_review_comment`-triggered workflows. The token has `pull-requests: write` permission, but the GitHub integration security model blocks this specific mutation unless the token is a PAT or App token.

**Which commands need the PAT:**
- Any command that resolves a thread needs it. These commands are `dismiss`, `false-positive`, `wont-fix`, and `fixed` as an inline reply (or with an `F<n>` that names an inline finding).
- The `authorize` job needs it for any inline reply. It also gates the `feedback-command` job, which handles `feedback`.
- Only `rescan`, `review-full`, `skip`, and `help` do not use the PAT.
- The reusable workflow declares the `github-token` secret as `required: true`. Pass a value even if you use only the commands that do not use it.

**Pass `actions-token` too, not only `github-token`.** Suppose your `secrets:` block sets `github-token` but omits `actions-token`. Then the `dismiss`, `dismiss-inline`, and `feedback-command` jobs have no `GITHUB_TOKEN` to use for their writes that do not resolve threads. They use the PAT for everything in that step. This is the `gchaix`-attribution bug that #734 fixed. If you pass both (as the current templates do), the PAT is used only for the thread-resolution mutation and the `authorize` check.

**The identity that owns the PAT is the identity that GitHub shows as the one that resolved the conversation.** That one mutation has no bot-attributed alternative. If you use the PAT of a maintainer, resolved threads show the name of that maintainer, even with `actions-token` configured. If this matters to your team, use a PAT from a dedicated machine or bot GitHub account.

**Create a PAT:**
- Classic PAT: Go to Settings → Developer settings → Personal access tokens → Tokens (classic). Grant the `repo` scope.
- Fine-grained PAT: Grant **Read and write** access to **Pull requests** and **Read** access to **Metadata** on the target repository.

Then add it as a repository secret with the name `GH_TOKEN` (Settings → Secrets and variables → Actions → New repository secret).

### 3. Verify your API key secret

The template accepts `secrets.AI_REVIEW_API_KEY` or `secrets.ANTHROPIC_API_KEY`. Either works without renaming. If you use a different provider, change the `provider` input to match.

### 4. Merge to your default branch

> **You must complete this step before the commands work.** GitHub runs `issue_comment` and `pull_request_review_comment` workflows from the **default branch** only. Suppose you add the workflow file in a PR. The automatic review (`pull_request` trigger) works immediately, but slash commands do not respond until that PR merges.

Commit and merge the workflow file. After it lands on your default branch, post `/ai-pr-review help` in any PR to verify.

> **Tip:** Suppose slash commands worked before you added the `GH_TOKEN` secret. In that case, thread resolution was probably failing without an error. After the merge, post `/ai-pr-review help` to confirm that the workflow runs.

## Commands

### `/ai-pr-review rescan`

Forces a full-diff re-review of the PR and bypasses the SHA watermark. Use this command when you want a fresh review after a series of small fixup commits.

On a fork PR that the engine paused at a cap, `rescan` also resumes it. One `rescan` by an owner, member or collaborator adds exactly one run, and `AI_FORK_MAX_COST_USD` (2 USD by default), to the budget of that PR. It does not reset the run count and does not change the author cap or the daily cap. The `slash-commands.yml` workflow reads `AI_FORK_POLICY` and `AI_FORK_TRUSTED_ASSOCIATIONS` from the repository variables of the calling repository, not from the environment of your review job. See [Fork pull requests](fork-prs).

### `/ai-pr-review review-full`

Starts a full-mode review that uses all agents. These agents include architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, and adversarial-general. This review takes longer and costs more than the default quick mode.

`/ai-pr-review full` is an alias for `review-full`. Both commands start the same run and get the same reaction and reply.

### `/ai-pr-review skip`

Adds the `skip-ai-review` label to the PR. The label stops the next automated review trigger. To enable automatic reviews again, remove the label manually.

### `/ai-pr-review dismiss`

Marks a specific AI review finding as a false positive. `false-positive` and `wont-fix` (below) use the exact same dismissal mechanics when you post them as a reply to an inline finding. This page describes `dismiss` first because thread resolution needs no learning-loop setup. `dismiss` is also an alias of `false-positive`. If `AI_FEEDBACK_LOOP` is on and the commenter is `OWNER` or `MEMBER`, it also writes an entry to the learning-loop store (see [Learning loop](learning-loop#supported-commands)).

#### For inline findings (reply on the review thread)

Post the command as a **reply to the inline review comment of the bot**. The workflow then does these steps:
1. It validates that `github-actions[bot]` posted the parent comment.
2. It resolves the review thread that contains the finding.
3. It checks whether any unresolved threads remain on the same review.
4. If all threads are resolved, it dismisses the `CHANGES_REQUESTED` review with an attribution message.
5. Suppose the command cleared the **last** active finding across **every** `CHANGES_REQUESTED` review on the PR (not only the review that the workflow dismisses), and the actor is `OWNER` or `MEMBER`. Then the bot also submits a new **APPROVE** review. See [Auto-approve on clear](#auto-approve-on-clear) below.

This process lets you dismiss findings one at a time. Suppose a review has three findings and only one is a false positive. If you dismiss that one, the `CHANGES_REQUESTED` state stays in place until you also resolve the remaining threads.

#### For body-level findings (top-level PR comment with `F<n>`)

Body-level findings appear in the `### Findings not attached to specific lines` section. Each finding has a **stable per-PR ID**, for example `**[F1]**` or `**[F2]**`. To dismiss one, post a comment like this:

```
/ai-pr-review dismiss F1
```

IDs are **PR-wide and stable across review cycles**. If `F1` was the ID of a finding in the first review, it is the ID of the same finding in every later review. New findings from later reviews get the next unused ID. The bot never re-uses an ID. A gap like `F1, F3` (no `F2`) means that a prior cycle dismissed `F2`.

The workflow accepts both `F1` and `[F1]` (the bracketed form in the review body).

Suppose you post `/ai-pr-review dismiss` without an ID as a top-level PR comment. The bot then replies with the list of active body-level finding IDs and the correct syntax.

When all **inline** review threads are resolved, the workflow dismisses the `CHANGES_REQUESTED` review automatically. This is the same behavior as the inline path, and it includes the auto-approve check described below.

### `/ai-pr-review false-positive [F<n>] [reason]`

Records the finding as a false positive in the learning loop. The `[reason]` is optional, but you should add it. It helps future reviews avoid the same finding in similar contexts.

**For inline findings:** Post the command as a reply on the inline review-comment thread of the AI. The command resolves the thread. It also dismisses the owning `CHANGES_REQUESTED` review after all of its threads are resolved. The mechanics are identical to `/ai-pr-review dismiss` (see above), and they include the auto-approve check. The workflow also extracts the source, file, and rule_id from the parent comment for the learning-loop entry.

**For body-level findings:** Post a top-level PR comment with the stable ID of the finding:
```
/ai-pr-review false-positive F2 documented via JSDoc, not the shortcode param block
```

This command requires `AI_FEEDBACK_LOOP=true` on the action input. It also requires a `GH_TOKEN` with `contents:write` on the feedback branch (default: `ai-pr-review-bot`). The workflow saves the entry to `.ai-pr-review/learnings.jsonl` on that branch.

### `/ai-pr-review wont-fix [F<n>] [reason]`

Records the finding as intentional (won't fix). Use this command when the finding is valid but the pattern is deliberate in your codebase. Examples are the intentional use of MD5 for non-security checksums and the intentional suppression of exceptions in a specific error handler.

**For inline findings:** Reply on the thread. The rules are the same as for `false-positive`, including thread resolution, review dismissal, and the auto-approve check.

**For body-level findings:** Post a top-level PR comment with the finding ID:
```
/ai-pr-review wont-fix F3 intentional behavior, see design doc
```

This command has the same setup requirements as `false-positive`.

### Auto-approve on clear

Suppose a `dismiss`, `false-positive`, or `wont-fix` reply clears the **last** unresolved finding across **every** bot-authored `CHANGES_REQUESTED` review on the PR. This includes reviews other than the review that the cleared finding belonged to. The bot then dismisses the remaining review or reviews and submits a new **APPROVE** review. The review decision of the PR then reaches `APPROVED`. Without this step, the decision stays at `REVIEW_REQUIRED` with only a dismissed review. The GitHub REST API has no endpoint to convert the state of an existing review. This step is therefore the only way to reach an approving state after a slash-command dismissal.

Only commenters with the `OWNER` or `MEMBER` repository association can trigger this step. This bar is one tier stricter than the `OWNER`, `MEMBER`, or `COLLABORATOR` bar that dismissal uses. It matches the trust level that the learning-loop commands already require for their persistent writes (`false-positive` and `wont-fix`). A `COLLABORATOR` can still dismiss, false-positive, and wont-fix findings normally. Their commands never trigger the auto-approve.

The check reads the review state again immediately before it dismisses or approves. If a concurrent push adds a finding between the triggering comment and the action of the bot, the approve stops.

An [`AI_APPROVAL_CEILING`](configuration#approval-ceiling) other than `approve` (issue #858) stops this whole escalation, whatever the trust level of the commenter. A configured ceiling forbids exactly a real `APPROVED` review. Ordinary per-review dismissal (thread resolution, stale-review cleanup) still happens normally. Only the extra approve step does not happen.

### `/ai-pr-review fixed [F<n>] [sha] [reason]` {#fixed-command}

Marks a finding as fixed. This is the opposite claim from `dismiss`, `false-positive`, and `wont-fix`. The finding was **correct**, and the code now addresses it. The finding was not wrong or intentional. Use this command, not `dismiss`, when you have fixed the issue. `dismiss` would misrepresent the outcome. `false-positive` and `wont-fix` would also teach the learning loop to stop flagging a pattern that was a real bug.

**For inline findings:** Reply on the thread. The command resolves the thread. It dismisses the owning `CHANGES_REQUESTED` review after all of its threads are resolved. These mechanics are the same as for `dismiss`. The command **does not** trigger the [auto-approve escalation](#auto-approve-on-clear) above, whatever your repository association is. A fix claim is not a verification of the fix. The approval must come from the next review run against the new commit, not from this command. The reply states this explicitly.

**For body-level findings:** Post a top-level PR comment with the stable ID of the finding:
```
/ai-pr-review fixed F3 a1b2c3d fixed by validating the input length
```
Body-level findings have no backing review thread, so nothing needs resolution. The reply only acknowledges the command. The next review run re-evaluates the finding like any other.

**The optional commit SHA** (`a1b2c3d` above) appears bare in the reply, so GitHub links it to the commit automatically. This gives a durable pointer to the fix for anyone who reads the thread later. The workflow does **not validate** the SHA against the repository. A mistyped or unrelated SHA shows as plain, unlinked text. The SHA has no role in thread resolution or review dismissal. Only the finding ID controls those actions.

**`fixed` never writes to the learning loop**, even with `AI_FEEDBACK_LOOP=true`. Unlike `false-positive` and `wont-fix`, it makes no claim about whether the pattern is a bug. If the workflow recorded it as a verdict, the review model would have no rule to act on. A reader could also misread the entry as a suppression signal for a finding that was correct.

### `/ai-pr-review feedback <text>`

Stores free-form feedback in the learning loop. The feedback does not belong to a specific finding verdict. Use it, for example, to note that a certain category of finding is too noisy for this repository.

**On Bitbucket**, the review run applies the command at the next run (see [Bitbucket setup](bitbucket-setup#dismissing-findings)). The text is required. A bare command gets a usage reply and saves nothing. An `F<n>` before the text is optional. When you give one, the entry records the source and file of that finding. The command uses the same authorization as `false-positive` and `wont-fix`: the owners file first, then the repository permission lookup. It saves the text only when `AI_FEEDBACK_LOOP=true` and `AI_BITBUCKET_VERDICTS=true`. The text passes the same sanitization as on GitHub.

### `/ai-pr-review explain [F<n>]`

Requests a more detailed explanation from the originating agent. This command is currently a stub. The workflow recognizes and acknowledges the command, but it does not yet call the agent again. It posts a canned reply.

**For inline findings:** Post the command as a reply on the inline review-comment thread of the AI.

**For body-level findings:** Post a top-level PR comment with the stable ID of the finding:
```
/ai-pr-review explain F2
```

### `/ai-pr-review revise [F<n>] <hint>`

Asks the originating agent to revise its finding with the hint that you provide. This command is currently a stub, in the same way as `explain`.

**For inline findings:** Post the command as a reply on the inline review-comment thread of the AI.

**For body-level findings:** Post a top-level PR comment with the stable ID of the finding and the hint:
```
/ai-pr-review revise F3 focus on the icon card variant specifically
```

### `/ai-pr-review help`

Posts the command list as a reply comment.

## Learning loop setup

The learning loop (`AI_FEEDBACK_LOOP=true`) stores feedback in a JSONL file on a dedicated git branch (`ai-pr-review-bot` by default). To enable it, do these steps:

1. Set `enable-feedback-loop: 'true'` in the inputs of the reusable slash-commands workflow. The template reads it from `vars.AI_REVIEW_FEEDBACK_LOOP`. This input controls all store writes and the `feedback-command` job.
2. Set `feedback-loop: 'true'` in the review action input. This is a separate input. It loads the stored context into the prompts of `rescan` and `review-full`.
3. Make sure that `GH_TOKEN` has `contents:write` on the feedback branch. The workflow creates the branch automatically on the first write.
4. To use a custom branch name, set the `AI_FEEDBACK_BRANCH` environment variable (default: `ai-pr-review-bot`). There is no `feedback-branch` action input.

For the full architecture and retention policy, see [Learning loop](learning-loop).

> **GitLab and Bitbucket:** On GitLab, the learning loop is a stub and feedback commands do nothing. On Bitbucket, the `false-positive` and `wont-fix` verdicts persist when `AI_FEEDBACK_LOOP=true` and `AI_BITBUCKET_VERDICTS=true`. See [Learning loop: Provider support](learning-loop#provider-support).

## Access control

Only users with the `OWNER`, `MEMBER`, or `COLLABORATOR` association on the repository can trigger commands. GitHub does **not** enforce this automatically. Without a guard, any authenticated user who can comment on a PR could trigger reviews. The way the guard works depends on the trigger event:

- **`issue_comment`-triggered jobs** (top-level PR comments) trust the `author_association` field from GitHub directly. A guard on the `if:` condition of the job does this.
- **`pull_request_review_comment`-triggered jobs** (inline thread replies: dismiss, false-positive, wont-fix, fixed, or feedback on an inline finding) do **not** trust `author_association`. This field is unreliable on this webhook type (issue #732). A confirmed organization member can arrive with an association value that fails the same guard. These jobs instead call a dedicated `authorize` job. That job checks the standing of the commenter live through the GitHub API. It uses `orgs/{org}/members/{actor}` for the OWNER/MEMBER equivalent and `repos/{repo}/collaborators/{actor}/permission` for the COLLABORATOR equivalent. The jobs then use its result as the gate.

## Feedback via emoji reactions

The workflow uses emoji reactions on the triggering comment to show status:

| Reaction | Meaning |
|---|---|
| 👀 | Command recognized, processing |
| 🚀 | Review started (rescan/review-full only) |
| 👍 | Command completed successfully |
| 😕 | Command failed or not applicable (for example, dismiss on a non-bot comment) |

## Default-branch dispatch behavior

The `slash-commands` job (and any `issue_comment` or `pull_request_review_comment` workflow) runs from the **default branch** of your repository, not the PR branch. This is a GitHub Actions platform behavior.

**What this means in practice:**
- Changes to slash-command behavior take effect only after you merge `ai-pr-review.yml` to your default branch.
- A PR that changes `ai-pr-review.yml` does not use its own updated slash-commands job while the PR is open. It uses the version on the default branch.
- The automatic PR review (`pull_request` trigger) in the same file runs immediately on any branch, including a PR branch. Only the comment-triggered jobs need a merge to the default branch.

## Customizing

The starter template has optional inputs for common customizations. A repository variable backs each input. You can set the variables in **Settings → Variables** without a change to the workflow file:

```yaml
provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}        # LLM provider
base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}                  # For openai-compatible/bedrock-proxy
image-tag: ${{ vars.AI_REVIEW_IMAGE_TAG || 'latest' }}          # Pin to a specific container version
review-mode-default: ${{ vars.AI_REVIEW_MODE_DEFAULT || '' }}  # Mode for rescan, '' defers to .github/ai-pr-review/policy.yml, else quick

# Analyzer / agent filtering (Python engine), honored on rescan and review-full
analyzers: ${{ vars.AI_REVIEW_ANALYZERS || '' }}                # Allowlist of analyzers (empty = all eligible)
exclude-analyzers: ${{ vars.AI_REVIEW_EXCLUDE_ANALYZERS || '' }}  # Denylist (e.g. 'phpstan,phpcs')
agents: ${{ vars.AI_REVIEW_AGENTS || '' }}                      # Allowlist of review agents
exclude-agents: ${{ vars.AI_REVIEW_EXCLUDE_AGENTS || '' }}      # Denylist of review agents

# Review tuning (honored on rescan and review-full)
max-diff-lines: ${{ vars.AI_REVIEW_MAX_DIFF_LINES || '5000' }}  # Skip review above this many diff lines
max-inline: ${{ vars.AI_REVIEW_MAX_INLINE || '25' }}            # Max inline comments per run
ignore-merge-commits: ${{ vars.AI_REVIEW_IGNORE_MERGE_COMMITS || 'true' }}  # Strip upstream base-branch merges
context-enrichment: ${{ vars.AI_REVIEW_CONTEXT_ENRICHMENT || 'true' }}      # tree-sitter symbol-context injection
model-standard: ${{ vars.AI_REVIEW_MODEL_STANDARD || '' }}      # Override the standard-tier model
model-premium: ${{ vars.AI_REVIEW_MODEL_PREMIUM || '' }}        # Override the premium-tier model
parallel: ${{ vars.AI_REVIEW_PARALLEL || 'true' }}              # Dispatch eligible agents concurrently
max-tokens-per-agent: ${{ vars.AI_REVIEW_MAX_TOKENS_PER_AGENT || '32768' }}  # Output-token budget per agent
enable-suggestions: ${{ vars.AI_REVIEW_ENABLE_SUGGESTIONS || 'true' }}  # Inline fix suggestions
fail-on-findings: ${{ vars.AI_REVIEW_SLASH_FAIL_ON_FINDINGS || 'false' }}  # Non-zero exit on qualifying findings. Own variable, 'false' by default: a failed run on this path misfires the failure-reaction step, not a required check
feedback-loop: ${{ vars.AI_REVIEW_FEEDBACK_LOOP || 'false' }}   # Load learning-loop context into agent prompts
token-usage-display: ${{ vars.AI_REVIEW_TOKEN_USAGE_DISPLAY || 'compact' }}  # Token-usage table rendering mode
token-usage-warn-usd: ${{ vars.AI_REVIEW_TOKEN_USAGE_WARN_USD || '1.00' }}   # Warn above this estimated USD spend
max-cost-usd: ${{ vars.AI_REVIEW_MAX_COST_USD || '0' }}         # Abort before any LLM call above this USD estimate (0 = disabled)
fail-on-cost-ceiling: ${{ vars.AI_REVIEW_FAIL_ON_COST_CEILING || 'false' }}  # Non-zero exit if the cost ceiling is exceeded
cost-ceiling-unpriced: ${{ vars.AI_REVIEW_COST_CEILING_UNPRICED || 'warn' }}  # 'block' skips the review when a model has no pricing entry
context-max-queries: ${{ vars.AI_REVIEW_CONTEXT_MAX_QUERIES || '200' }}      # Max ripgrep symbol-lookup queries per run
exclude-patterns: ${{ vars.AI_REVIEW_EXCLUDE_PATTERNS || '' }}  # Comma-separated globs to exclude from review
exclude-patterns-mode: ${{ vars.AI_REVIEW_EXCLUDE_PATTERNS_MODE || 'append' }}  # 'append' or 'replace'
analyzer-diff-scope: ${{ vars.AI_REVIEW_ANALYZER_DIFF_SCOPE || 'cap' }}      # How out-of-diff analyzer findings are handled
suppress-walkthrough: ${{ vars.AI_REVIEW_SUPPRESS_WALKTHROUGH || 'false' }}  # Leave the per-file Walkthrough table out of the summary comment
policy-source: ${{ vars.AI_REVIEW_POLICY_SOURCE || 'base-ref' }}              # Where policy.yml is read from
```

The four `analyzers` and `agents` inputs match the main action inputs that v1.6.0 added. The workflow now also forwards them through `rescan` and `review-full`. For example, a project can exclude `phpstan,phpcs` on the main review. The manual rescans then also skip them. Set the matching `AI_REVIEW_*` repository variables once, and both paths stay in sync.

The review-tuning inputs above (issues #863 and #865) close a class of bug. A repository variable took effect in the automatic `pull_request`-triggered review, but `rescan` and `review-full` ignored it without an error. Those two commands always ran with the hardcoded defaults of `container-action`, whatever the repository variable said. `fail-on-findings` is the one exception to note. It reads its own variable, `AI_REVIEW_SLASH_FAIL_ON_FINDINGS`, which defaults to `'false'`. The main review job reads `AI_REVIEW_FAIL_ON_FINDINGS` and defaults to `'true'`. The two variables are separate, so a repository that sets the review gate does not also turn it on for rescans. A non-zero exit on this comment-triggered path fails the workflow run and misfires the failure reaction. On the `pull_request` trigger, the same exit gates a required check.

The workflow does not forward every `container-action` input. The source of truth for which inputs it forwards, and why, is `tests/python/test_slash_commands_container_action_parity.py`. The build fails if a `container-action` input is neither forwarded from the review step nor listed in its `_EXEMPT_INPUTS` dict with a documented reason. The reasons are: GitHub-context plumbing that the workflow already forwards under another name, an input that cannot apply to a comment-triggered context, or an input that no `AI_REVIEW_*` variable exposes on any review path yet. Read that file, not a hardcoded count here. The exempt set can change without a change to this document.

The reusable workflow file (`.github/workflows/slash-commands.yml` in this repository) documents the complete list of inputs.

## Architecture: reusable workflow

The slash command system is a GitHub Actions [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows). The `slash-commands` job in the unified `ai-pr-review.yml` calls it through `workflow_call`:

```
Consumer repo                          ai-pr-review repo
┌─────────────────────────────┐        ┌────────────────────────────────┐
│ ai-pr-review.yml            │        │ .github/workflows/             │
│                             │        │   slash-commands.yml           │
│ jobs:                       │        │ (authorize, handle-command,    │
│   review:                   │        │  dismiss-*, feedback-command)  │
│     if: pull_request        │        │                                │
│     runs-on: ubuntu-latest  │        │ • command parsing              │
│     steps: [container-action│        │ • help / skip / rescan /       │
│                             │ ──────>│   review-full dispatch         │
│   slash-commands:           │  call  │ • dismiss: GraphQL thread      │
│     if: issue_comment ||    │        │   resolution + review dismiss  │
│         pr_review_comment   │        └────────────────────────────────┘
│     uses: slash-commands.yml│
└─────────────────────────────┘
```

**Benefits:**
- One file configures both the automatic review and slash commands.
- Bug fixes and new commands ship upstream. Consumers get them automatically on their next run.
- Consumers never need to understand or maintain the complex GraphQL logic of the dismiss job.
- The review action and the slash-command inputs share the same repository variables, so they cannot drift apart.

## Extending the command surface

To add custom commands that apply only to your repository, use one of these options:

1. **Add a separate job** in your consumer workflow. The job handles your custom commands before or after the call to the reusable workflow.
2. **Open an issue** on the ai-pr-review repository. Propose that the project add the command upstream, if it would help other consumers.
