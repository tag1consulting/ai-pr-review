---
layout: default
title: GitLab Setup
parent: Configuration
nav_order: 5
render_with_liquid: false
---

# GitLab CI/CD setup

`ai-pr-review` supports GitLab merge requests (MRs). It uses the same container image as GitHub Actions and Bitbucket Pipelines. The GitLab path posts one summary comment per MR and updates it in place on later runs. It also posts inline discussion threads on changed lines. These threads can include suggestion fences that the MR author applies with one click.

## What works

- Summary comment upsert (one note per MR, updated on each run)
- Inline MR discussion threads on changed lines
- Suggestion fences in inline discussions (GitLab's `suggestion` syntax)
- Incremental-diff SHA watermark (same HTML-comment marker as GitHub)
- Stale discussion resolution: the review resolves prior bot inline threads automatically. It resolves only threads with a first note from the bot and the `<!-- ai-pr-review-inline -->` marker.
- Cross-run finding dedup (issue #710): the review compares an unchanged finding with this bot's open prior discussions (same file, within 3 lines, compatible category). It does not repost the finding and it keeps the existing discussion open. For a changed finding, the review updates the matched discussion note in place. If the severity increases, the review also adds a reply that notes the change. This dedup is narrower than GitHub's canonical-review reuse. GitLab has no dismiss, false-positive, wont-fix, or fixed verdicts, so GitLab has no suppression. Set `AI_GITLAB_CROSS_RUN_DEDUP=false` to disable the dedup.
- All existing AI agents and static analyzers (same container image)
- Self-hosted GitLab instances through `GITLAB_API_URL`
- Automatic retry on transient GitLab API errors (429, 502, 503, 504, and transient network errors)

## What does not work on GitLab

- Slash-command triggers (see [alternatives](#slash-command-alternatives) below)
- MR approval and unapproval: GitLab never posts a real approval state

## Slash command alternatives

GitLab CI has no `issue_comment` event trigger. The `/ai-pr-review` slash commands (rescan, review-full, skip, dismiss, false-positive, wont-fix, fixed, feedback) work on GitHub PRs. They are not available on GitLab MRs.

**Workarounds for common operations:**

| GitHub slash command | GitLab equivalent |
|---|---|
| `/ai-pr-review rescan` | Set `FORCE_FULL_DIFF=true` as a CI/CD variable, then re-run the pipeline. Or delete the summary comment on the MR to reset the SHA watermark. |
| `/ai-pr-review review-full` | Set `AI_REVIEW_MODE=full` as a CI/CD variable (persists for all future runs until changed back). |
| `/ai-pr-review skip` | Add `[skip ci]` to a commit message, or set a CI/CD variable `SKIP_AI_REVIEW=true` and add a `rules:` condition to the pipeline job. |
| `/ai-pr-review dismiss` | Resolve individual discussion threads manually in the GitLab UI. |
| `/ai-pr-review fixed` | Resolve the discussion thread manually in the GitLab UI after you commit the fix. GitLab has no equivalent for a citation of the fixing commit. |

For more advanced automation, GitLab supports [pipeline triggers via API](https://docs.gitlab.com/ee/ci/triggers/) and [webhook events on MR notes](https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html#comment-events). You can connect these to an external service that triggers review pipelines with custom variables. This needs additional infrastructure beyond the CI config.

## One-time setup

### 1. Create a project access token

In your GitLab project, go to **Settings → Access tokens** and create a new project access token:

- **Role:** Developer (minimum role to post notes and discussions)
- **Scopes:** `api` (required to create notes and MR discussions)

Store the token in a safe place. GitLab shows it only one time.

You can also use a personal access token with the `api` scope, limited to the project. The Developer role can post notes and discussions.

> **Token type detection:** The script detects the token format automatically. `glpat-*` tokens use the `PRIVATE-TOKEN` header. `glcbt-*` tokens use the `JOB-TOKEN` header. All other tokens (for example, OAuth2 tokens from `glab auth login`) use the `Authorization: Bearer` header.

> **Note on `CI_JOB_TOKEN`:** The built-in CI job token has limited scopes. It usually cannot create MR notes or discussions. The script uses `CI_JOB_TOKEN` if `GITLAB_TOKEN` is not set, but this will likely fail with 403. Use a project access token for full functionality.

### 2. Set CI/CD variables

In your GitLab project, go to **Settings → CI/CD → Variables** and add:

| Name | Masked? | Protected? | Value |
|---|---|---|---|
| `GITLAB_TOKEN` | Yes | Optional | The project access token from step 1 |
| `ANTHROPIC_API_KEY` | Yes | Optional | Your Anthropic API key (or swap for your provider) |
| `AI_PROVIDER` | No | No | `anthropic` (default). Alternatives: `openai`, `google`, `bedrock-proxy` |
| `AI_REVIEW_MODE` | No | No | Empty (default): defer to `.ai-pr-review/policy.yml` route matching if present, else `quick`. Set to `full` to override. |

### 3. Copy the starter pipeline

Copy [`examples/pipelines/.gitlab-ci.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/pipelines/.gitlab-ci.yml) to the root of your repo as `.gitlab-ci.yml`. Commit and push the file. The review runs on every MR open and update through merge request pipelines.

## Environment variables the review reads

The starter pipeline translates the native GitLab CI variables to the canonical variables of the review. If you write your own pipeline, make sure these variables are set:

| Review var | Source (GitLab CI) |
|---|---|
| `VCS_PROVIDER` | Must be set to `gitlab` |
| `PR_NUMBER` | `$CI_MERGE_REQUEST_IID` |
| `MR_IID` | `$CI_MERGE_REQUEST_IID` |
| `BASE_REF` | `$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` |
| `HEAD_SHA` | `$CI_COMMIT_SHA` |
| `GITLAB_PROJECT_ID` | `$CI_PROJECT_ID` |
| `GITLAB_DIFF_BASE_SHA` | `$CI_MERGE_REQUEST_DIFF_BASE_SHA` |
| `GITLAB_TOKEN` | CI/CD variable (masked) |
| `GITHUB_REPOSITORY` | `$CI_PROJECT_PATH` (used as fallback project identifier) |
| `AI_PROVIDER` | CI/CD variable (default `anthropic`) |
| `AI_REVIEW_MODE` | CI/CD variable (default empty: defer to `policy.yml` route matching, else `quick`. Set to `full` for deep agents) |
| `ANTHROPIC_API_KEY` (or equivalent) | CI/CD variable (masked) |
| `GITLAB_BOT_USERNAME` | (optional) CI/CD variable. If unset, the review detects it with `GET /user`. If the lookup fails, the review trusts no summary or skip marker, so it runs in full on every push. Set it when the review posts as a custom bot account. |

> **Note:** The review needs `GITLAB_DIFF_BASE_SHA` or `CI_MERGE_REQUEST_DIFF_BASE_SHA`. If neither is set, the review stops with a configuration error.

> **Forcing a full re-review:** The `ai-review-rescan` label mechanism works only in GitHub Actions. For GitLab, set `FORCE_FULL_DIFF=true` as a CI/CD variable for one run. Or delete the summary comment on the MR to reset the SHA watermark.

## `GIT_STRATEGY: clone` and `GIT_DEPTH: "0"` are required

The default GitLab CI clone is shallow. It can miss the base branch. The review does not run `git fetch` itself. It expects `origin/<BASE_REF>` to exist as a local ref. If the ref is missing, the `git diff` command fails and the review stops with a `GitDiffError`. Set `GIT_STRATEGY: clone` and `GIT_DEPTH: "0"` in your pipeline YAML to prevent this. The starter pipeline sets both values.

## Self-hosted GitLab

For a self-hosted GitLab instance, set the `GITLAB_API_URL` CI/CD variable to the API base URL of your instance:

```
GITLAB_API_URL=https://gitlab.example.com/api/v4
```

The default is `https://gitlab.com/api/v4`.

## Security considerations

### Secret exposure to pipeline contributors

`GITLAB_TOKEN` and `ANTHROPIC_API_KEY` (or your provider key) are exposed as CI/CD variables to **any pipeline run triggered from a branch in this project**. This includes MRs opened by any user with branch-push access.

A contributor with push access to any branch can change `.gitlab-ci.yml` in their MR and steal these secrets. This is the classic "pwn-request" pattern.

Mitigations:
- **Use a dedicated project access token** with minimum scope: `api` on the reviewed project only. Do not use a personal token with broader access.
- **Mark variables as "Protected"** if your workflow needs reviews only on protected branches.
- **Restrict who can push branches** in **Settings → Repository → Protected branches**.
- **Do not use this setup on a public open-source project** without additional safeguards.

### `GITLAB_TOKEN` scope

Use the minimum scope required (`api`). If the token has broader access (for example, admin rights), a token compromise has a much larger blast radius.

## Troubleshooting

### `ERROR: gl_api POST /projects/.../notes -> 401`

The project access token is missing, wrong, or expired. Check **Settings → Access tokens** for the status and expiry of the token.

### `ERROR: gl_api POST /projects/.../notes -> 403`

The token exists but does not have the `api` scope, or the role of the token is too low (it needs at least Developer). If you use `CI_JOB_TOKEN`, it probably does not have the required scopes. Use a project access token instead.

### `ERROR: gl_api POST /projects/.../discussions -> 400`

The inline discussion position is not valid. The line can be missing from the MR diff (for example, after a rebase). The review puts the finding in the summary comment body instead. This is normal and not fatal.

### `WARNING: No diff base SHA available`

`GITLAB_DIFF_BASE_SHA` / `CI_MERGE_REQUEST_DIFF_BASE_SHA` is not set. This happens when the pipeline is not a merge request pipeline. Make sure your `rules:` block includes `$CI_PIPELINE_SOURCE == "merge_request_event"`. Without the base SHA, the review stops with a configuration error.

### `ERROR: git diff against 'origin/<ref>...<sha>' failed`

The review does not run `git fetch`. The `origin/<BASE_REF>` ref is not in the clone, so the `git diff` command fails and the review stops. Set `GIT_STRATEGY: clone` and `GIT_DEPTH: "0"` in your pipeline YAML. If you use a custom pipeline, fetch the base branch before you start the review.

### Nothing posts, review exits 0

The diff is probably larger than `MAX_DIFF_LINES` (default 5000). The pipeline log shows `Review skipped: diff too large (N lines > M)`. The review also posts a skip note on the MR to explain the skip.
