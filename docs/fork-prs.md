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

## The fork profile

Under `auto`, a fork pull request from an author who is not trusted runs under the fork profile. The profile changes four things:

- Only analyzers with a trusted or isolated configuration run. These are phpcs, phpstan, checkov, tflint, ruff (with `--isolated`), trufflehog (without the repository config), docs-api-check, docs-ref-check, docs-drift-check, dep-exists, cve-check, and suppression-check. The analyzers eslint, golangci-lint, semgrep, shellcheck, hadolint, and kube-linter are skipped. The review comment names the skipped analyzers that had files to check.
- Suppression rules come from the base branch, never from the fork tree. This applies to `.ai-pr-review/suppressions.json` and its legacy path.
- `AI_ANALYZER_ENV_PASSTHROUGH` has no effect. The analyzers get no extra environment variable, and no proxy setting.
- The review pauses when `.git/config` holds a persisted credential. The checks are: an `http.*.extraheader` key, a `credential.*` key, an `includeIf` key, or a token in a remote URL. Set `persist-credentials: false` on the checkout step to fix the pause. The newer `actions/checkout` versions keep the token in a separate file and add an `includeIf` key, so the default checkout pauses a fork review.

A rule in a suppressions file that has an invalid regular expression is dropped alone. The other rules stay. This applies to every run.

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

## What this does not do

- **The gate covers the engine only.** Every other step in your workflow runs on every fork push, whatever the approval state is. This includes `actions/checkout` of the fork head, any step that runs analyzers on the fork tree, and a `uses: ./container-action` that is not restored from the base branch. Keep those steps free of secrets, or give them the same gate.
- **Only the `review` command is gated.** Slash commands that make no model call are not.
- **The approval names the head commit that the label event carried.** A push while the maintainer was reading, before the label was added, is approved with the new head. The workflow rejects the approval if the pull request moved after the label event. It cannot know which commit the maintainer read. Check the head commit again just before you add the label. A stronger design makes the maintainer name the commit (for example a comment that holds the full commit id). That design is not built.
- **Any token with `statuses: write` on the base repository can create the approval status.** The workflow checks the person who adds the label. It does not check who created the status, and it does not accept a bot as the person who labels. A workflow with write-all token permissions that runs fork code could create the status itself.
- **The review still runs in the job that holds the secrets.** A flaw in the engine, or in a tool that reads the checked-out tree, is not contained by this gate. The reviewed commit is code that a maintainer chose to approve.
- **A skip comment stays after an approved run.** The "review is paused" comment is not removed when the real review posts.
- **Self-hosted runners can keep files between jobs.** Use a clean runner for fork pull requests.
