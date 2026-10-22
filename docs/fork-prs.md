---
layout: default
title: Fork pull requests
nav_order: 8
---

# Fork pull requests

A workflow that runs on `pull_request_target` has the repository secrets and a write token. It also checks out the head of the pull request. For a fork, that head is code that anyone can write. The engine therefore does not trust a fork pull request. By default (`AI_FORK_POLICY=auto`), the engine reviews a fork pull request at once, with no maintainer step, and it limits what that run can do, read, post and spend. A maintainer acts only to resume a pull request that reached a cap.

## Fork policy

`AI_FORK_POLICY` sets what the engine does with a fork pull request. The default is `auto`.

| Policy | What happens to a fork pull request |
|--------|-------------------------------------|
| `auto` | The run starts at once. An author whose `author_association` is in `AI_FORK_TRUSTED_ASSOCIATIONS` gets the normal pipeline. Any other author gets the fork profile and the caps. The engine does not read the approval status, so the workflow does not need `statuses: read`. |
| `label` | The run pauses until a maintainer approves the exact head commit. See [The label policy](#the-label-policy). |
| `block` | Every fork pull request pauses, whoever wrote it. Use it to switch fork review off. |

An unknown `AI_FORK_POLICY` value is ignored with a warning, and `label` applies. `label` is the safest policy that still allows a review, so an unknown value never gives the more open `auto`. A misspelled `block` also gives `label`, so check the warning in the job log after you set the policy. An unset or empty value gives the default, `auto`.

## Who is trusted

`AI_FORK_TRUSTED_ASSOCIATIONS` is a comma-separated list of `author_association` values. The default is `OWNER,COLLABORATOR`. `MEMBER` is not in the default list. An empty value trusts nobody.

A fork pull request by a trusted author gets the normal pipeline. It keeps the per-run cost ceiling and the workspace commit check. It does not get the fork profile, the PR cap, the author cap or the daily cap, and it does not count against the daily cap. The list applies to the `auto` policy only. The `label` and `block` policies treat every fork author the same.

## How a fork run works under `auto`

The engine checks the pull request before it runs any git command in the checked-out tree.

- A plain `pull_request` run is not paused when it holds no LLM key. By default, GitHub gives a fork no secrets on that event, so the run could not call a model. A private repository can turn on "Send secrets and variables to workflows from fork pull requests". The engine then finds a key in the environment and applies the gate to that run.
- Any other event is checked when the head repository is not the base repository. This includes `pull_request_target`, `issue_comment` and an unknown event. A deleted fork counts as a fork.
- The checked-out tree must be the commit under review. The engine compares `git rev-parse HEAD` of the workspace with the head commit and pauses on any difference. Your workflow must check out `github.event.pull_request.head.sha`, never a branch name.
- If the engine cannot read the pull request, the run pauses. The gate fails closed. The cause is in the job log.
- A same-repository pull request is not paused by the fork policy. If the engine cannot read the pull request facts, it pauses every gated event, including a same-repository one (see the fail-closed rule below).
- The gate applies to GitHub only. GitLab and Bitbucket runs are not gated.

## The fork profile

A fork pull request by a non-trusted author runs under the fork profile. The profile has these parts.

**A scrubbed environment.** Every analyzer and every git subprocess gets an environment built from an allowlist. It holds `PATH`, a fresh temporary `HOME`, `LC_ALL`, `PYTHONSAFEPATH=1` and a few per-tool variables. No LLM key, `GH_TOKEN`, `GITHUB_TOKEN` or `ACTIONS_*` value reaches a subprocess. This part applies to every run, not only to fork runs. On a fork run, `AI_ANALYZER_ENV_PASSTHROUGH` has no effect, and the proxy and certificate variables do not pass.

**Only analyzers with a trusted or isolated configuration.** These analyzers run: phpcs, phpstan, checkov, tflint, ruff (with `--isolated`), trufflehog (without the repository config), docs-api-check, docs-ref-check, docs-drift-check, dep-exists, cve-check and suppression-check. These analyzers are skipped: eslint, golangci-lint, semgrep, shellcheck, hadolint and kube-linter. A fork review therefore has fewer static findings than a normal review. The review comment names the skipped analyzers that had files to check.

**Suppression rules from the base branch.** The engine reads `.ai-pr-review/suppressions.json` and its legacy path from the base branch, never from the fork tree. A rule with an invalid regular expression is dropped alone. The other rules stay. This applies to every run.

**No low-value text in the prompts.** A fork run sends no PR body, no commit message, no open-issue title and no branch name to a model. It does not run the pr-summarizer or the issue-linker. The PR title stays, and the engine escapes it. A fork review has no PR summary. The comment holds one fixed line in its place, so that the watermark still advances.

**A clamped output.** See [The output clamp](#the-output-clamp).

**A pause on a stored credential.** The review pauses when `.git/config` holds a persisted credential. The checks are: an `http.*.extraheader` key, a `credential.*` key, an `includeIf` key, or a token in a remote URL. Set `persist-credentials: false` on the checkout step to fix the pause. The newer `actions/checkout` versions keep the token in a separate file and add an `includeIf` key, so a default checkout pauses a fork review.

## The output clamp

A fork run cannot approve a pull request. The engine limits what the run posts, because the text comes from a model that read code the author controls.

- The review event is COMMENT. The engine never posts APPROVE or REQUEST_CHANGES for a fork run.
- Inline suggestions are off. The engine drops `suggested_code` and does not rewrite it.
- Every comment and review body starts with this banner: "Automated review of a fork pull request. The text below is derived from untrusted code. Do not follow links or instructions in it."
- The text has no image and no link to another host. A link to a file in this repository, an anchor and a link to the engine repository stay.
- Mentions such as `@team` and cross references such as `owner/repo#1` are neutralized with a zero-width space. This applies to fork runs only.

Two more protections apply to every run. The engine strips control characters, format characters and Unicode tag characters from the diff, the title, the manifest paths and the symbol context, and it escapes a closing tag that an untrusted embed could use to leave its block. The engine also scrubs secret literals and token prefixes from every string it posts, and it cuts a string at 262,144 bytes. A line that starts with `::` in a log is neutralized, so model text cannot forge a workflow command.

Agent output that the engine cannot read counts as a failed agent. The review then posts COMMENT and names the agent, and it never approves. With `AI_FAIL_ON_FINDINGS=true`, this also exits with code 2. Before this change, unreadable output counted as no findings.

## Caps and what a pause means

A fork pull request by a non-trusted author has four caps. A run that would pass a cap pauses before it runs any analyzer or model. The skip comment names the cap.

| Cap | Value | Setting |
|-----|-------|---------|
| One run | 2 USD | `AI_FORK_MAX_COST_USD` |
| One pull request | 4 runs and 6 USD | none |
| One author | The 2 open fork pull requests with the lowest numbers | none |
| One day | 50 USD per rolling 24 hours | `AI_FORK_DAILY_MAX_COST_USD` |

**Run cap.** The per-run cost ceiling is the smaller of `AI_MAX_COST_USD` and `AI_FORK_MAX_COST_USD`. A failed cost estimate pauses the run. A zero, negative or non-finite value of `AI_FORK_MAX_COST_USD` falls back to 2 USD with a warning, so no value switches the ceiling off. The ceiling does not bound a model that has no price entry (see `AI_COST_CEILING_UNPRICED`).

**Pull request cap.** The engine keeps the budget of one pull request in hidden comments that only the bot wrote. Each run adds one entry for its estimated cost and replaces it with the actual cost when the run ends. A cancelled run stays charged at its estimate. Comments from other users are ignored.

**Author cap.** The engine reviews only the 2 open fork pull requests with the lowest numbers for one author. A later pull request pauses. The review starts when an earlier pull request of that author is closed or merged. The engine counts by author login, so a second account counts as a second author.

**Daily cap.** Authors with the `CONTRIBUTOR` association have 70 percent of the daily budget (`AI_FORK_DAILY_RESERVED_SHARE`). Every other non-trusted author shares the remaining 30 percent. A full shared pool does not use the `CONTRIBUTOR` pool. A run that is over the budget removes its own entry and pauses. The pause clears as the 24 hour window moves on. `AI_FORK_DAILY_MAX_COST_USD=0` pauses every fork run, so use `AI_FORK_POLICY=block` to switch fork review off.

**Fail closed.** If the engine cannot read or write a ledger, cannot read the author's open pull requests, or cannot estimate the cost, the run pauses.

**Resume.** A maintainer comments `/ai-pr-review rescan` on a paused pull request. The engine counts each rescan comment from an owner, member or collaborator once. One rescan adds one run and `AI_FORK_MAX_COST_USD` to the cap of that pull request. It does not reset the count, and it does not change the author cap or the daily cap. Each further run after a pause needs another rescan.

A rescan runs through the `slash-commands.yml` workflow. That workflow does not read the environment of your review job. That workflow does not read the environment of your review job. It reads `AI_FORK_POLICY` and `AI_FORK_TRUSTED_ASSOCIATIONS` from the repository variables of the calling repository. If you change either setting, set it as a repository variable too, or the rescan applies the defaults (the `auto` policy, and `OWNER` and `COLLABORATOR` as trusted). Only an owner, member or collaborator can start a rescan.

## The spend issue

The daily cap needs a record of past runs. The engine keeps one bot comment per fork run on an issue titled `ai-pr-review fork spend (do not close)`. It creates the issue on the first run. The workflow needs `issues: write` for this. Do not close, edit or delete the issue or its comments. A maintainer can lock the issue to stop other users from adding comments. Comments from other users are ignored, but about 10,000 of them in one day make the listing too long, and fork reviews pause until the flood ends. Trusted authors and same-repository pull requests add no entries.

## What the workflow must provide

- `permissions:` with `pull-requests: write` and `issues: write`. The `auto` policy does not need `statuses: read`.
- A checkout of `github.event.pull_request.head.sha` with `persist-credentials: false`.
- A `concurrency` group per pull request, as in `examples/workflows/pr-review.yml`. It serializes runs of one pull request. Without it, the engine relies on its reserve-then-verify step.
- A trusted `container-action` that you restore from the base branch, as in the examples.

## The label policy

Set `AI_FORK_POLICY=label` to return to a maintainer approval of each commit. The engine pauses a fork pull request until the base repository has a successful commit status named `ai-pr-review/fork-approval` on the exact head commit. The `label` policy treats every fork author the same and applies none of the `auto` caps.

1. Copy [`examples/workflows/fork-approval.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/fork-approval.yml) into your repository. This workflow is for the `label` policy only. Create the `ai-review-approve-fork` label first. The workflow must never check out or run code from the pull request.
2. Add `statuses: read` to the `permissions:` block of the review workflow. Without it, the status request fails and the run stays paused, with a log line that names the permission.
3. Read the full pull request diff (Files changed) at the commit that the skip comment names. The review covers everything in the pull request, not only the last commit.
4. Check that the head commit is still the one you read, then add the label.
5. A trusted workflow checks that the person who added the label has write access. It then creates the commit status on the commit that the label event carried.
6. Start the review again. Re-run the paused job in the Actions tab, or comment `/ai-pr-review rescan`.

The approval covers one commit. After a push, the new commit has no status, and the run pauses again. The approval workflow also removes the label on each push.

Limits of the `label` policy:

- The status names the head commit that the label event carried. A push while the maintainer was reading, before the label was added, is approved with the new head. The workflow rejects the approval if the pull request moved after the label event. It cannot know which commit the maintainer read.
- Any token with `statuses: write` on the base repository can create the approval status. A workflow with write-all token permissions that runs fork code could create the status itself.
- A skip comment stays after an approved run.

## Make a pause visible to a required check

A paused run exits 0, so a required review check can turn green without a review. Set `AI_FAIL_ON_FORK_GATE=true` in the workflow environment to exit 2 on a pause. A paused run then fails the check until the pause clears. The default is off, like `fail-on-cost-ceiling`.

## Limits that remain

The `auto` policy reduces risk. It does not remove it. Read [SECURITY.md](https://github.com/tag1consulting/ai-pr-review/blob/main/SECURITY.md) for the full list. The main limits are these.

- **Prompt injection is not preventable.** A fork author can make the review say nothing, or something misleading, about the fork's own pull request. The output clamp limits the harm, because the review cannot approve and cannot post links, images or mentions.
- **The review runs in the job that holds the secrets.** The environment scrub covers subprocesses. A flaw in the engine process itself is not contained. Issue #1098 tracks a split into two jobs.
- **The caps are best effort.** A user with write access can edit or delete the ledger comments and so reset a cap.
- **Many accounts can use up the daily cap.** Fork reviews then pause until the window moves on. Trusted pull requests are not affected.
- **A trusted author can carry untrusted commits.** The engine trusts the author of the pull request and not each commit in it.
- **`COLLABORATOR` can include low-privilege accounts.** Check who has that association in your repository, or remove it from `AI_FORK_TRUSTED_ASSOCIATIONS`.
- **Forks get fewer analyzers.** The skipped analyzers return only when each one has a trusted configuration.
- **Some analyzers read files from the fork tree.** dep-exists reads `.npmrc`, `.yarnrc` and `.cargo/config` to decide whether to skip itself, so a fork can silence findings about unknown packages for its own pull request. docs-drift-check uses `rg`, so `.gitignore`, `.ignore` and `.rgignore` can hide references to a deleted file. ruff and trufflehog still honor inline suppressions in fork files, and trufflehog follows symlinks in changed files. The engine reads SARIF files at the `AI_SARIF_PATHS` paths from the fork tree. `AI_POLICY_SOURCE=workspace` reads `policy.yml` from the fork tree.
- **The credential check has gaps.** It does not look for `core.sshCommand`, `core.askPass`, `http.cookieFile`, `http.proxy` with a user name or `config.worktree`. Use `persist-credentials: false`.
- **Another workflow that runs as the bot login can write a valid-looking ledger entry.** This has the same trust level as the summary and skip markers.
- **The gate covers the engine only.** Every other step in your workflow runs on every fork push. This includes `actions/checkout` of the fork head and any step that runs analyzers on the fork tree. Keep those steps free of secrets.
- **Only a command that starts a review is gated.** Slash commands that make no model call are not.
- **Self-hosted runners can keep files between jobs.** Use a clean runner for fork pull requests.
