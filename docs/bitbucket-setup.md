---
layout: default
title: Bitbucket Setup
parent: Configuration
nav_order: 4
---

# Bitbucket Cloud Pipelines setup

`ai-pr-review` supports Bitbucket Cloud PRs via the same container image used
for GitHub Actions. The Bitbucket path posts a single summary comment per PR
(updated in place on subsequent runs). Findings eligible for an inline anchor
render as [Code Insights](https://support.atlassian.com/bitbucket-cloud/docs/code-insights/)
annotations directly on the PR diff, and everything else renders as markdown
bullets inside the comment body.

Bitbucket Cloud's own API *can* do real inline PR comments, threaded replies,
and thread resolution. This is not a platform limitation. `ai-pr-review`
deliberately doesn't use them: Code Insights annotations are natively
idempotent (re-posting the same finding never duplicates it) and require no
thread-resolution machinery at all, since annotations aren't repliable. See
[ADR 0005](adr/0005-bitbucket-code-insights-not-inline-comment-threads) for
the full reasoning behind that choice.

## What works

- Summary comment upsert (single comment per PR, updated on each run)
- Inline findings via Code Insights annotations, rebuilt from scratch every
  run (`AI_BITBUCKET_CODE_INSIGHTS`, default `true`). Every active finding
  still renders as a bullet in the summary comment too — one that also got
  an annotation just gets a shortened bullet (no remediation sub-bullet,
  since that detail already lives on the diff) rather than being omitted
  from the comment entirely. Each annotation carries the same `[F<n>]`
  token as the summary comment's findings, so you can reference it there.
  If Code Insights isn't available on your workspace's plan (a 403/404 on
  the report API), every finding still renders with its full bullet in the
  summary comment body, exactly as it did before this feature existed.
  Nothing is silently dropped.
- Setting the PR's own reviewer state (approved / changes requested) to
  match the review's decided outcome (`AI_BITBUCKET_REVIEW_STATE`, default
  `true`). A downgrade to `COMMENT` clears any prior approve/request-changes
  state rather than leaving a stale badge from an earlier run.
  **Upgrading into this version**: since this defaults on, you start
  getting real approve/request-changes state immediately with no opt-in
  step. If a branch restriction counts this bot's approval toward a merge
  requirement, or blocks merges on "changes requested", review that rule
  before upgrading. Set `AI_BITBUCKET_REVIEW_STATE=false` to keep the
  prior heading-text-only behavior.
- Cross-run dedup: a finding dismissed via the summary comment's hidden
  verdicts marker is excluded from the next run's Code Insights report as
  long as it stays at the same file and line (part of the same
  `AI_BITBUCKET_CODE_INSIGHTS` flag, see [issue
  #839](https://github.com/tag1consulting/ai-pr-review/issues/839)).
  Unlike GitHub's fuzzy match (same file, within 3 lines, compatible
  category), Bitbucket has no comment threads to fuzzy-match against, so
  suppression here is exact-fingerprint-only. A dismissed finding whose
  line shifts by even one on a later push is treated as a new finding
  again.
- Dismissing/suppressing a finding via a comment command
  (`AI_BITBUCKET_VERDICTS`, default `false` — see [Dismissing
  findings](#dismissing-findings) below)
- Incremental-diff SHA watermark (a hidden reference-link marker — Bitbucket's
  renderer shows an HTML comment as literal text instead of hiding it, unlike
  GitHub/GitLab, so Bitbucket uses a different marker form; see [Version
  History → v2.6.1](version-history/v2.6.1))
- All existing AI agents and static analyzers (same container image, same
  review logic)
- Provider-auto retry on transient Bitbucket API errors (408/429/500-504)

### Code Insights annotation category mapping

Each finding's category maps to one of Code Insights' three
`annotation_type` values. The line is drawn at: `VULNERABILITY` = an attacker
can exploit it, `BUG` = wrong at runtime, `CODE_SMELL` = maintainability.
`test-gap` is grouped under `BUG` (not `CODE_SMELL`) because Bitbucket
renders `BUG` more prominently, and a missing test on a security-relevant
path is not merely cosmetic.

| `annotation_type` | Categories |
|---|---|
| `VULNERABILITY` | `authz`, `injection`, `secret`, `dependency-cve` |
| `BUG` | `edge-case`, `test-gap` |
| `CODE_SMELL` | `architecture-coupling`, `observability`, `docs`, `lint`, `other` |

Severity maps 1:1: Critical→`CRITICAL`, High→`HIGH`, Medium→`MEDIUM`,
Low→`LOW`.

### Concurrency

Each run tears down and rebuilds the Code Insights report from scratch
(delete, then recreate, then post its annotations). Two runs on the same
PR overlapping closely enough to interleave their own delete/recreate
cycles can leave the report in an inconsistent state, the same class of
risk GitHub/GitLab reviews already have (see [Features → Quiet
reruns](features#quiet-reruns-github)'s Concurrency note). If your
pipeline pushes frequently enough for this to matter, configure a
Bitbucket Pipelines concurrency setting keyed on the PR so only one
review runs against it at a time.

## Dismissing findings

Enable with `AI_BITBUCKET_VERDICTS=true` (default `false`) and optionally
`AI_BITBUCKET_VERDICT_MIN_ROLE` (`read`, `write`, or `admin`; default
`write`).

**Bitbucket Pipelines has no `issue_comment`-equivalent trigger** (see
"What does not work" below), so unlike GitHub's instant webhook-driven
response, a verdict command does not take effect until **the next review
run** — the review run itself is what polls the PR's comments for pending
commands. If you want it applied immediately, re-run the pipeline manually
from **Pipelines → \[run\] → Rerun** in the Bitbucket UI rather than
waiting for the next push.

**Syntax:** always a **top-level PR comment** referencing a finding's
`F<n>` token from the summary comment — there is no inline-comment-reply
form the way GitHub has, because Bitbucket findings anchor to the diff via
Code Insights annotations, which aren't repliable:

```
/ai-pr-review false-positive F3 not exploitable, input is sanitized upstream
/ai-pr-review wont-fix F7 accepted risk for this release
/ai-pr-review fixed F12 a1b2c3d
```

`dismiss` is accepted as an alias for `false-positive`. Multiple commands
in one comment (one per line) are all applied, same as GitHub/GitLab.

**Authorization is fail-closed**: the commenter's Bitbucket repository
permission is checked against `AI_BITBUCKET_VERDICT_MIN_ROLE` before a
command is applied; a permission-lookup failure rejects the command
rather than accepting it. The bot replies to every processed command —
applied, rejected for insufficient access, or rejected because the
permission check itself couldn't be completed (distinguished in the
reply text so you know whether to fix your access or just retry) — so a
command is never silently ignored.

**Not yet supported:** writing to the cross-repo learning-loop store
(the `<repo-feedback>` context injected into future prompts) — see
[Learning loop → Provider support](learning-loop#provider-support). A
verdict still suppresses the finding on this PR via the hidden verdicts
marker; it just doesn't teach future reviews on other PRs the way a
GitHub dismiss does.

## What does not work on Bitbucket

- Writing to the cross-repo learning-loop store from a verdict command
  (see [Dismissing findings](#dismissing-findings) above)
- Slash commands other than dismiss/false-positive/wont-fix/fixed
  (`explain`, `revise`, `feedback`, `rescan`, `review-full`, `skip`):
  Bitbucket Pipelines has no `issue_comment` equivalent, so these have no
  trigger at all. Only the verdict commands work, and only because the
  review run itself polls for them (see [Dismissing
  findings](#dismissing-findings) above) rather than reacting to the
  comment as it's posted.
- The large-diff "skip" comment (the review still exits cleanly and logs
  a warning, but no comment is posted)
- Collapsed/expandable sections (the PR-summary Walkthrough, and the
  token-usage table under `token-usage-display: full`, render as flat,
  always-expanded content on Bitbucket; Bitbucket Cloud renders no HTML at
  all in comments, so `<details>` isn't an option — see
  [BCLOUD-20231](https://jira.atlassian.com/browse/BCLOUD-20231)). The
  default `token-usage-display: compact` is a single plain line, so this
  only matters if you opt into `full`.

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
| `AI_FAIL_ON_FINDINGS` | No | `true` (default, see the starter pipeline). Exit code 2 when Critical/High findings block approval, failing the pipeline step. Set to `false` to always exit 0. |
| `AI_REVIEW_IMAGE_TAG` | No | Container tag to pull, e.g. `latest`. Required, no default. The starter pipeline's `image.name` field templates this itself via Bitbucket's `${{VAR}}` syntax, which cannot resolve a secured variable at all, so this one must stay non-secured. |
| `AI_BITBUCKET_VERDICTS` | No | `false` (default). Set to `true` to enable dismiss/false-positive/wont-fix/fixed comment commands — see [Dismissing findings](#dismissing-findings). |
| `AI_BITBUCKET_VERDICT_MIN_ROLE` | No | `write` (default). Minimum Bitbucket repository permission (`read`, `write`, or `admin`) required to apply a verdict command. Only meaningful when `AI_BITBUCKET_VERDICTS=true`. |
| `AI_BITBUCKET_REVIEW_STATE` | No | `true` (default). Set to `false` to stop the bot from calling Bitbucket's approve/request-changes endpoints — the decided outcome still renders as heading text in the summary comment either way. |

### 3. Grant PR scopes

The API token's effective scopes follow the user's permissions. The bot
user must have at least:

- **Repository:Read** on the repo being reviewed
- **Pull request:Write** on the repo (to create and update comments)

### 4. Copy the starter pipeline

Copy [`examples/pipelines/bitbucket-pipelines.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/pipelines/bitbucket-pipelines.yml)
to the root of your repo as `bitbucket-pipelines.yml`, commit, and push. The
review fires on every PR open/update. If you hand-edit the `image:` block,
keep the `${{AI_REVIEW_IMAGE_TAG}}` template form: Bitbucket's `image.name`
field requires that syntax and rejects a bare `$AI_REVIEW_IMAGE_TAG`.

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

`ai_pr_review` never fetches from `origin` itself — it assumes
`origin/<BASE_REF>` already exists as a local ref (see issue #702). On
GitHub Actions, `actions/checkout` with `fetch-depth: 0` creates that ref as
part of its own full clone. Bitbucket Pipelines does a single-branch PR
clone, so the starter template's own script step fetches the base branch
explicitly before invoking the tool:

```
git fetch origin "$BITBUCKET_PR_DESTINATION_BRANCH":"refs/remotes/origin/$BITBUCKET_PR_DESTINATION_BRANCH"
```

That fetch needs the base branch's history to actually be present in the
clone to succeed, which the default shallow Pipelines clone does not
guarantee — set `clone.depth: full` at the top of `bitbucket-pipelines.yml`
(the starter does this) to avoid that.

If `origin/<BASE_REF>` is still missing when the tool runs (e.g. a custom
pipeline that skips this fetch step), `git diff` against it fails and the
tool now raises a `GitDiffError` and aborts the pipeline with a non-zero
exit — it no longer silently reports "no changed files" and skips the
review without saying why.

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

### `ERROR: git diff against 'origin/<ref>...<sha>' failed`

The starter template's `git fetch` step (see "`clone.depth: full` is
required" above) either didn't run or didn't find the base branch. Confirm
`clone.depth: full` is set and that `BITBUCKET_PR_DESTINATION_BRANCH` is the
branch you expect.

### Nothing posts, review exits 0

Most likely the diff is over `MAX_DIFF_LINES` (default 5000). The pipeline
logs will show `::warning::Diff is too large`. On Bitbucket, no skip comment
is posted (by design — no comment is preferable to a noisy empty one).
