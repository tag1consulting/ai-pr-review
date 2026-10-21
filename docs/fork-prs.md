---
layout: default
title: Fork pull requests
nav_order: 8
---

# Fork pull requests

A workflow that runs on `pull_request_target` has the repository secrets and a write token, and it checks out the head of the pull request. For a fork, that head is code that anyone can write. The engine therefore pauses a fork pull request until a maintainer approves the exact commit.

## What the engine does

The engine checks the pull request before it runs any git command in the checked-out tree.

- A plain `pull_request` run is never paused. GitHub gives a fork no secrets on that event.
- Any other event is paused when the head repository is not the base repository. This includes `pull_request_target`, `issue_comment`, and an unknown event. A deleted fork counts as a fork.
- A paused run posts a skip comment and exits 0. The comment names the commit and the label to add.
- The checked-out tree must be the approved commit. The engine compares `git rev-parse HEAD` of the workspace with the approved commit and pauses on any difference. Your workflow must check out `github.event.pull_request.head.sha`, never a branch name.
- If the engine cannot read the pull request or the statuses, the run stays paused. The gate fails closed.
- The gate applies to GitHub only. GitLab and Bitbucket runs are not gated.

## How a maintainer approves

1. Read the diff of the commit.
2. Add the `ai-review-approve-fork` label.
3. A trusted workflow checks that the person who added the label has write access. It then creates a commit status named `ai-pr-review/fork-approval` on the commit that the label event saw.
4. When the approval workflow has succeeded, start the review again. Re-run the paused job in the Actions tab, or comment `/ai-pr-review rescan`. The label does not start the review by itself, because the review and the approval are two separate workflows.

The approval covers one commit. After a push, the new commit has no status, and the run pauses again. The workflow also removes the label on each push, so the next approval is a new label event.

Copy [`examples/workflows/fork-approval.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/fork-approval.yml) into your repository. Create the `ai-review-approve-fork` label first. The workflow must never check out or run code from the pull request.

## Cost ceiling

An approved fork pull request gets a default cost ceiling of 2 USD when you set none. Set `max-cost-usd` to use your own ceiling for all pull requests. Set `AI_FORK_MAX_COST_USD` to change the default for forks, or to `0` to switch the default off. An unusable value falls back to 2 USD and never switches the ceiling off.

## What this does not do

- The review still runs in the same job as the secrets. A flaw in the engine, or in a tool that reads the checked-out tree, is not contained by this gate. The reviewed commit is code that a maintainer chose to approve.
- A maintainer can approve a commit without reading it. A person with write access can also create the status directly.
- A race remains between the maintainer reading the diff and the label event. The workflow rejects the approval when the pull request has moved, but a push in the moment before that check is approved. Read the commit that the skip comment names.
- Self-hosted runners can keep files between jobs. Use a clean runner for fork pull requests.
