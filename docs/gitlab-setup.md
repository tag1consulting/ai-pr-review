---
layout: default
title: GitLab Setup
parent: Getting Started
nav_order: 3
render_with_liquid: false
---

# GitLab CI/CD setup

`ai-pr-review` supports GitLab merge requests via the same container image
used for GitHub Actions and Bitbucket Pipelines. The GitLab path posts a
summary comment per MR (updated in place on subsequent runs) and inline
discussion threads on specific changed lines, with optional suggestion
fences that the MR author can apply with one click.

## What works

- Summary comment upsert (single note per MR, updated on each run)
- Inline MR discussion threads on changed lines
- Suggestion fences in inline discussions (GitLab's `suggestion` syntax)
- Incremental-diff SHA watermark (same HTML-comment marker as GitHub)
- Stale discussion resolution (prior bot inline threads are auto-resolved; gated on bot first-note authorship and the `<!-- ai-pr-review-inline -->` marker)
- MR approval / unapproval based on risk classification (no Critical/High
  findings → approve; Critical/High present → remove prior approval)
- All existing AI agents and static analyzers (same container image)
- Self-hosted GitLab instances via `GITLAB_API_URL`
- Provider-auto retry on transient GitLab API errors (408/429/500-504)

## What does not work on GitLab

- Slash-command triggers — see [alternatives](#slash-command-alternatives)
  below

## Slash command alternatives

GitLab CI has no `issue_comment` event trigger, so the `/ai-pr-review`
slash commands (rescan, review-full, skip, dismiss) that work on GitHub
PRs are not available on GitLab MRs.

**Workarounds for common operations:**

| GitHub slash command | GitLab equivalent |
|---|---|
| `/ai-pr-review rescan` | Set `FORCE_FULL_DIFF=true` as a CI/CD variable, then re-run the pipeline. Or delete the summary comment on the MR to reset the SHA watermark. |
| `/ai-pr-review review-full` | Set `AI_REVIEW_MODE=full` as a CI/CD variable (persists for all future runs until changed back). |
| `/ai-pr-review skip` | Add `[skip ci]` to a commit message, or set a CI/CD variable `SKIP_AI_REVIEW=true` and add a `rules:` condition to the pipeline job. |
| `/ai-pr-review dismiss` | Resolve individual discussion threads manually in the GitLab UI. |

For more advanced automation, GitLab supports
[pipeline triggers via API](https://docs.gitlab.com/ee/ci/triggers/) and
[webhook events on MR notes](https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html#comment-events),
which could be wired to an external service that triggers review pipelines
with custom variables. This requires additional infrastructure beyond the
CI config.

## One-time setup

### 1. Create a project access token

In your GitLab project, go to **Settings → Access tokens** and create a
new project access token:

- **Role:** Developer (minimum for posting notes and discussions)
- **Scopes:** `api` (required for creating notes and MR discussions)

Store the token somewhere safe — GitLab shows it only once.

Alternatively, use a personal access token with `api` scope, scoped to
the project. The Developer role can post notes and discussions. For MR
approval to work, the token's user must be an eligible approver under
the project's approval rules — if approval fails, the script logs a
warning and continues.

> **Token type detection:** The script auto-detects the token format:
> `glpat-*` tokens use the `PRIVATE-TOKEN` header, `glcbt-*` tokens use
> the `JOB-TOKEN` header, and all other tokens (e.g. OAuth2 tokens from
> `glab auth login`) use the `Authorization: Bearer` header.

> **Note on `CI_JOB_TOKEN`:** The built-in CI job token has limited
> scopes and typically cannot create MR notes or discussions. The script
> falls back to `CI_JOB_TOKEN` if `GITLAB_TOKEN` is not set, but this
> will likely fail with 403. Use a project access token for full
> functionality.

### 2. Set CI/CD variables

In your GitLab project, go to **Settings → CI/CD → Variables** and add:

| Name | Masked? | Protected? | Value |
|---|---|---|---|
| `GITLAB_TOKEN` | Yes | Optional | The project access token from step 1 |
| `ANTHROPIC_API_KEY` | Yes | Optional | Your Anthropic API key (or swap for your provider) |
| `AI_PROVIDER` | No | No | `anthropic` (default). Alternatives: `openai`, `google`, `bedrock-proxy` |
| `AI_REVIEW_MODE` | No | No | `quick` (default) or `full` |

### 3. Copy the starter pipeline

Copy [`examples/pipelines/.gitlab-ci.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/pipelines/.gitlab-ci.yml)
to the root of your repo as `.gitlab-ci.yml`, commit, and push. The review
fires on every MR open/update via merge request pipelines.

## Environment variables the review reads

The starter pipeline translates GitLab's native CI variables to the
review's canonical contract. If you write your own pipeline, ensure these
are set:

| Review var | Source (GitLab CI) |
|---|---|
| `VCS_PROVIDER` | Must be set to `gitlab` |
| `PR_NUMBER` | `$CI_MERGE_REQUEST_IID` |
| `BASE_REF` | `$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` |
| `HEAD_SHA` | `$CI_COMMIT_SHA` |
| `GITLAB_PROJECT_ID` | `$CI_PROJECT_ID` |
| `GITLAB_MR_DIFF_BASE_SHA` | `$CI_MERGE_REQUEST_DIFF_BASE_SHA` |
| `GITLAB_TOKEN` | CI/CD variable (masked) |
| `GITHUB_REPOSITORY` | `$CI_PROJECT_PATH` (used as fallback project identifier) |
| `AI_PROVIDER` | CI/CD variable (default `anthropic`) |
| `AI_REVIEW_MODE` | CI/CD variable (default `quick`; set to `full` for deep agents) |
| `ANTHROPIC_API_KEY` (or equivalent) | CI/CD variable (masked) |
| `GITLAB_BOT_USERNAME` | (optional) CI/CD variable; if unset, auto-detected via `GET /user` |

> **Note:** `GITLAB_MR_DIFF_BASE_SHA` is required for inline discussion
> threads. Without it, all findings are posted in the summary comment
> body instead of as inline discussions.

> **Forcing a full re-review:** The `ai-review-rescan` label mechanism
> is GitHub Actions-only. For GitLab, set `FORCE_FULL_DIFF=true` as a
> CI/CD variable for a single run, or delete the summary comment on the
> MR to reset the SHA watermark.

## `GIT_STRATEGY: clone` and `GIT_DEPTH: "0"` are required

The default GitLab CI clone is shallow and may not include the base
branch. If `git fetch origin $BASE_REF` fails (e.g. on a shallow clone
missing the base branch), the script emits a warning and falls back to
existing local refs; if the base ref is still unreachable it aborts with
an error. Set `GIT_STRATEGY: clone` and `GIT_DEPTH: "0"` in your pipeline
YAML (the starter does this) to avoid this.

## Self-hosted GitLab

For self-hosted GitLab instances, set the `GITLAB_API_URL` CI/CD variable
to your instance's API base URL:

```
GITLAB_API_URL=https://gitlab.example.com/api/v4
```

The default is `https://gitlab.com/api/v4`.

## Security considerations

### Secret exposure to pipeline contributors

`GITLAB_TOKEN` and `ANTHROPIC_API_KEY` (or your provider key) are exposed
as CI/CD variables to **any pipeline run triggered from a branch in this
project**. This includes MRs opened by any user with branch-push access.

A contributor with push access to any branch can modify `.gitlab-ci.yml`
in their MR to exfiltrate these secrets — this is the classic "pwn-request"
pattern.

Mitigations:
- **Use a dedicated project access token** with minimum scope: `api` on
  the reviewed project only, not a personal token with broader access.
- **Mark variables as "Protected"** if your workflow only needs reviews
  on protected branches.
- **Restrict who can push branches** in **Settings → Repository →
  Protected branches**.
- **Do not use this setup on a public open-source project** without
  additional safeguards.

### `GITLAB_TOKEN` scope

Use the minimum scope required (`api`). If the token has broader access
(e.g. admin rights), a token compromise has a much larger blast radius.

## Troubleshooting

### `ERROR: gl_api POST /projects/.../notes -> 401`

The project access token is missing or wrong, or the token has expired.
Check **Settings → Access tokens** for the token's status and expiry.

### `ERROR: gl_api POST /projects/.../notes -> 403`

The token exists but lacks `api` scope, or the token's role is too low
(needs at least Developer). If using `CI_JOB_TOKEN`, it likely lacks
the required scopes — use a project access token instead.

### `ERROR: gl_api POST /projects/.../discussions -> 400`

The inline discussion position is invalid — the line may not exist in
the MR's diff (e.g. due to a rebase). The finding will fall back to the
summary comment body. This is normal and non-fatal.

### `WARNING: No diff base SHA available`

`GITLAB_MR_DIFF_BASE_SHA` / `CI_MERGE_REQUEST_DIFF_BASE_SHA` is not set.
This happens when the pipeline is not a merge request pipeline. Ensure your
`rules:` block includes `$CI_PIPELINE_SOURCE == "merge_request_event"`.
Without the base SHA, inline discussions are skipped and all findings go
to the summary comment.

### `WARNING: git fetch failed`

Usually harmless if the base branch is already present in the clone. If
the review aborts with `ERROR: origin/<ref> is not reachable`, set
`GIT_STRATEGY: clone` and `GIT_DEPTH: "0"` in your pipeline YAML.

### Nothing posts, review exits 0

Most likely the diff is over `MAX_DIFF_LINES` (default 5000). The pipeline
logs will show `WARNING: Diff is too large`. A skip note will be posted to
the MR explaining the skip.
