---
layout: default
title: Fork pull requests
nav_order: 8
---

# Fork pull requests

A workflow that runs on `pull_request_target` has the repository secrets and a write token, and it checks out the head of the pull request. For a fork, that head is code that anyone can write. The engine therefore pauses a fork pull request until a maintainer approves one exact head commit (the `label` policy, the default).

## Fork policy

`AI_FORK_POLICY` sets what the engine does with a fork pull request. The default is `label`.

| Policy | What happens to a fork pull request |
|--------|-------------------------------------|
| `label` | The run pauses until a maintainer approves the head commit (the approval described below). This is the maintainer-label policy. |
| `auto` | The run starts at once. An author whose `author_association` is in `AI_FORK_TRUSTED_ASSOCIATIONS` gets the normal pipeline. Any other author gets a fork profile. The engine does not read the approval status, so the workflow does not need `statuses: read`. |
| `block` | Every fork pull request pauses, whoever wrote it. |

`AI_FORK_TRUSTED_ASSOCIATIONS` is a comma-separated list. The default is `OWNER,COLLABORATOR`. `MEMBER` is not in the default list. An empty value trusts nobody. The list applies to the `auto` policy only. The `label` and `block` policies treat every fork author the same. An unknown `AI_FORK_POLICY` value is ignored with a warning, and `label` applies.

Under `auto`, this release only chooses the fork profile for a non-trusted author. The caps, the scrubbed environment, the dropped text and the output restrictions of that profile are separate changes. Do not set `auto` until they ship. Every allowed fork run, trusted or not, keeps the 2 USD default cost ceiling and the workspace commit check.

## What the engine does

The engine checks the pull request before it runs any git command in the checked-out tree.

- A plain `pull_request` run is not paused when it holds no LLM key. By default, GitHub gives a fork no secrets on that event, so the run could not call a model. A private repository can turn on "Send secrets and variables to workflows from fork pull requests". The engine then finds a key in the environment and applies the gate to that run.
- Any other event is paused when the head repository is not the base repository. This includes `pull_request_target`, `issue_comment`, and an unknown event. A deleted fork counts as a fork.
- A paused run posts a skip comment. The comment names the commit and the label to add. The run exits 0 unless you set `AI_FAIL_ON_FORK_GATE=true` (see below).
- The checked-out tree must be the approved commit. The engine compares `git rev-parse HEAD` of the workspace with the approved commit and pauses on any difference. Your workflow must check out `github.event.pull_request.head.sha`, never a branch name.
- If the engine cannot read the pull request or the statuses, the run stays paused. The gate fails closed. The cause is in the job log.
- A same-repository pull request is never paused and never reads statuses.
- The gate applies to GitHub only. GitLab and Bitbucket runs are not gated.

## Permissions

The workflow that runs the review needs `statuses: read` for a fork pull request, because the engine reads the approval status. Without it, the status request fails and the run stays paused, with a log line that names the permission. A same-repository pull request does not need it. If your workflow sets a `permissions:` block, add `statuses: read`.

## How a maintainer approves

1. Read the full pull request diff (Files changed) at the commit that the skip comment names. The review covers everything in the pull request, not only the last commit, so a harmless top commit does not make the rest safe.
2. Check that the head commit is still the one you read.
3. Add the `ai-review-approve-fork` label.
4. A trusted workflow checks that the person who added the label is a user with write access. It then creates a commit status named `ai-pr-review/fork-approval` on the commit that the label event carried.
5. When the approval workflow has succeeded, start the review again. Re-run the paused job in the Actions tab, or comment `/ai-pr-review rescan`. The label does not start the review by itself, because the review and the approval are two separate workflows.

The approval covers one commit. After a push, the new commit has no status, and the run pauses again. The workflow also removes the label on each push, so the next approval is a new label event.

Copy [`examples/workflows/fork-approval.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/fork-approval.yml) into your repository. Create the `ai-review-approve-fork` label first. The workflow must never check out or run code from the pull request.

## Make the pause visible to a required check

A paused run exits 0, so a required review check can turn green without a review. Set `AI_FAIL_ON_FORK_GATE=true` in the workflow environment to exit 2 on a pause. A paused run then fails the check until a maintainer approves. The default is off, like `fail-on-cost-ceiling`.

## Cost ceiling

An approved fork pull request gets a default cost ceiling of 2 USD when you set none. Set `max-cost-usd` to use your own ceiling for all pull requests. Set `AI_FORK_MAX_COST_USD` to change the default for forks, or to `0` to switch the default off. An unusable value falls back to 2 USD and never switches the ceiling off. The ceiling does not bound a model that has no price entry (see `AI_COST_CEILING_UNPRICED`).

## Budget for one fork pull request

With `AI_FORK_POLICY=auto`, a fork pull request by a non-trusted author gets at most 4 review runs and 6 USD in total. A run that would pass either cap is paused before it runs any analyzer or model. The skip comment names the cap. The cap is fixed and has no setting.

The engine keeps the budget in hidden comments that only the bot wrote. Each run adds one entry for its estimated cost and replaces it with the actual cost when the run ends. A cancelled run stays charged at its estimate. Comments from other users are ignored. If the engine cannot read or write the budget, or cannot estimate the cost, the run is paused.

A maintainer resumes a paused pull request with `/ai-pr-review rescan`. The engine counts each rescan comment from an owner, member or collaborator once. One rescan adds one run and `AI_FORK_MAX_COST_USD` (2 USD by default) to the cap. It does not reset the count. A user with write access can delete the bot comments and so reset the budget.

For a non-trusted fork run, the per-run ceiling is the smaller of `AI_MAX_COST_USD` and `AI_FORK_MAX_COST_USD`.

## Caps for one author and for one day

With `AI_FORK_POLICY=auto`, two more caps apply to fork pull requests by non-trusted authors.

The engine reviews only the 2 open fork pull requests with the lowest numbers for one author. A later pull request is paused, and the skip comment names the cap. The review starts when an earlier pull request of that author is closed or merged. The engine counts by author login, so a second account counts as a second author.

The engine also keeps a daily budget of 50 USD per rolling 24 hours (`AI_FORK_DAILY_MAX_COST_USD`). Authors with the `CONTRIBUTOR` association have 70 percent of it (`AI_FORK_DAILY_RESERVED_SHARE`). Every other non-trusted author shares the remaining 30 percent. A full shared pool does not use the `CONTRIBUTOR` pool. The engine keeps one bot comment per run on an issue titled `ai-pr-review fork spend (do not close)`. It creates the issue on the first run, and the workflow needs `issues: write` for this. Do not close, lock or edit the issue. Comments from other users are ignored. A paused run adds no entry. A run that is over the budget removes its own entry and pauses, and the comment says that the pause clears as the 24 hour window moves on. A rescan does not change the author cap or the daily budget. If the engine cannot read or write the issue or the pull request list, the run is paused.

Trusted authors and same-repository pull requests do not count against either cap. A user with write access can edit or delete the spend comments. Enough accounts can use up the daily budget and pause fork reviews until the window moves on.

## What this does not do

- **The gate covers the engine only.** Every other step in your workflow runs on every fork push, whatever the approval state is. This includes `actions/checkout` of the fork head, any step that runs analyzers on the fork tree, and a `uses: ./container-action` that is not restored from the base branch. Keep those steps free of secrets, or give them the same gate.
- **Only the `review` command is gated.** Slash commands that make no model call are not.
- **The approval names the head commit that the label event carried.** A push while the maintainer was reading, before the label was added, is approved with the new head. The workflow rejects the approval if the pull request moved after the label event. It cannot know which commit the maintainer read. Check the head commit again just before you add the label. A stronger design makes the maintainer name the commit (for example a comment that holds the full commit id). That design is not built.
- **Any token with `statuses: write` on the base repository can create the approval status.** The workflow checks the person who adds the label. It does not check who created the status, and it does not accept a bot as the person who labels. A workflow with write-all token permissions that runs fork code could create the status itself.
- **The review still runs in the job that holds the secrets.** A flaw in the engine, or in a tool that reads the checked-out tree, is not contained by this gate. The reviewed commit is code that a maintainer chose to approve.
- **A skip comment stays after an approved run.** The "review is paused" comment is not removed when the real review posts.
- **Self-hosted runners can keep files between jobs.** Use a clean runner for fork pull requests.
