---
layout: default
title: Bitbucket Setup
parent: Getting Started
nav_order: 2
---

# Bitbucket Cloud Pipelines setup

`ai-pr-review` supports Bitbucket Cloud PRs via the same container image used
for GitHub Actions. The Bitbucket path posts a single summary comment per PR
(updated in place on subsequent runs), with all findings rendered as markdown
bullets inside the comment body. Inline review comments and Code Insights
annotations are not currently available on the Bitbucket path.

## What works

- Summary comment upsert (single comment per PR, updated on each run)
- Incremental-diff SHA watermark (the same HTML-comment marker trick used on
  GitHub)
- All existing AI agents and static analyzers (same container image, same
  review logic)
- Provider-auto retry on transient Bitbucket API errors (408/429/500-504)

## What does not work on Bitbucket

- Inline review comments (deferred; all findings render inside the summary
  body)
- `REVIEW_TARGET=standalone` mode (Bitbucket Cloud has no Issues product —
  the script exits with an error if you set this)
- APPROVE / REQUEST_CHANGES PR events (Bitbucket has different endpoints
  for approve/request-changes and the feature is optional)
- Slash-command triggers (Bitbucket Pipelines has no `issue_comment`
  equivalent; the review always runs on PR create/push)
- The large-diff "skip" comment (the review still exits cleanly and logs
  a warning, but no comment is posted)

## One-time setup

### 1. Create a bot user and Atlassian API token

Either use a dedicated service account or your personal account. At
<https://id.atlassian.com/manage-profile/security/api-tokens>, create a new
API token and store it somewhere safe — Atlassian shows it only once.

### 2. Set repository variables

In your Bitbucket repo, go to **Repository settings → Pipelines → Repository
variables** and add:

| Name | Secured? | Value |
|---|---|---|
| `BITBUCKET_EMAIL` | No | Atlassian account email of the bot user |
| `BITBUCKET_API_TOKEN` | Yes | The API token from step 1 |
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key (or swap for your provider) |
| `AI_PROVIDER` | No | `anthropic` (default). Alternatives: `openai`, `google`, `bedrock-proxy` |
| `AI_REVIEW_MODE` | No | `quick` (default) or `full` |

### 3. Grant PR scopes

The API token's effective scopes follow the user's permissions. The bot
user must have at least:

- **Repository:Read** on the repo being reviewed
- **Pull request:Write** on the repo (to create and update comments)

### 4. Copy the starter pipeline

Copy [`examples/pipelines/bitbucket-pipelines.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/pipelines/bitbucket-pipelines.yml)
to the root of your repo as `bitbucket-pipelines.yml`, commit, and push. The
review fires on every PR open/update.

### 5. Enable Pipelines

In **Repository settings → Pipelines → Settings**, toggle Pipelines on if it
is not already enabled.

## Environment variables the review reads

The starter pipeline translates Bitbucket's native env vars to the review's
canonical contract. If you write your own pipeline, ensure these are set:

| Review var | Source (Bitbucket Pipelines) |
|---|---|
| `VCS_PROVIDER` | Must be set to `bitbucket` |
| `PR_NUMBER` | `$BITBUCKET_PR_ID` |
| `BASE_REF` | `$BITBUCKET_PR_DESTINATION_BRANCH` |
| `HEAD_SHA` | `$BITBUCKET_COMMIT` |
| `GITHUB_REPOSITORY` | `${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO_SLUG}` |
| `BITBUCKET_EMAIL` | Repo variable |
| `BITBUCKET_API_TOKEN` | Repo variable (secured) |
| `AI_PROVIDER` | Repo variable (default `anthropic`) |
| `ANTHROPIC_API_KEY` (or equivalent) | Repo variable (secured) |

> **Note:** `GITHUB_REPOSITORY` is reused as a generic `owner/repo` identifier
> so the same env contract works for both providers. You can alternatively
> set `BITBUCKET_WORKSPACE` and `BITBUCKET_REPO_SLUG` explicitly — the script
> prefers those if both are set.

## `clone.depth: full` is required

The default Bitbucket Pipelines clone is shallow and may not include the base
branch. If `git fetch origin $BASE_REF --depth=50` fails (e.g. on a shallow clone
missing the base branch), the script emits a warning and falls back to
existing local refs; if the base ref is still unreachable it aborts with an
error. Set `clone.depth: full` at the top of `bitbucket-pipelines.yml`
(the starter does this) to avoid this.

## Security considerations

### Secret exposure to pipeline contributors

`BITBUCKET_API_TOKEN` and `ANTHROPIC_API_KEY` (or your provider key) are
exposed as secured repo variables to **any pipeline run triggered from a
branch in this repository**. This includes PRs opened by any user with
branch-push access.

A contributor with push access to any branch can modify `bitbucket-pipelines.yml`
in their PR to exfiltrate these secrets — this is the classic "pwn-request"
pattern. Bitbucket Cloud's "do not expose secured variables to forks" setting
protects against external forks, but **not against in-repo branches**.

Mitigations:
- **Use a dedicated bot user** with minimum scope: Pull request:Write on the
  reviewed repo only, not workspace-wide admin access.
- **Restrict who can push branches** in **Repository settings → Branch
  restrictions**. Pipelines runs are limited to users who can push the
  triggering branch.
- **Enable manual approval** for pipelines triggered by non-maintainer
  contributions (**Repository settings → Pipelines → Settings →
  "Require manual step approval"**).
- **Do not use this setup on a public open-source repo** without additional
  safeguards — any fork contributor could open a PR against your repo.

### `BITBUCKET_API_TOKEN` scope

Use the minimum scope required (Repository:Read + Pull request:Write). If the
bot user has broader Workspace or Project admin rights, a token compromise has
a much larger blast radius.

## Troubleshooting

### `ERROR: bb_api POST /repositories/.../comments -> 401`

The bot user's API token is missing or wrong, or the user lacks Pull
request:Write on the repo. Double-check **Repository settings → Access
management** for the bot user.

### `ERROR: bb_api POST /repositories/.../comments -> 403`

The API token exists but the user does not have write access to comments.
Check **Workspace settings → Members** and **Repository settings → User and
group access**.

### `WARNING: git fetch failed; attempting to proceed with existing local refs.`

Usually harmless if the base branch is already present in the clone. If the
review aborts with `ERROR: origin/<ref> is not reachable`, set
`clone.depth: full` in your pipeline YAML.

### `ERROR: Standalone review mode is not supported for VCS_PROVIDER=bitbucket`

You have `REVIEW_TARGET=standalone` set somewhere. Remove it — standalone
mode is GitHub-only (Bitbucket Cloud has no Issues product).

### Nothing posts, review exits 0

Most likely the diff is over `MAX_DIFF_LINES` (default 5000). The pipeline
logs will show `::warning::Diff is too large`. On Bitbucket, no skip comment
is posted (by design — no comment is preferable to a noisy empty one).
