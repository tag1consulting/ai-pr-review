# Starter workflows and pipelines

Copy these files into your repository to enable AI PR/MR reviews.

## Files

| File | Purpose |
|---|---|
| `workflows/pr-review.yml` | GitHub Actions: **unified single-file setup** — automatic review on PR open/sync AND slash commands (`/ai-pr-review rescan`, dismiss, etc.) in one workflow. **Canonical template** — every input is wired to a `vars.AI_REVIEW_*` repo variable with a safe fallback. |
| `workflows/comment-triggers.yml` | GitHub Actions: **standalone slash-command wrapper** (two-file setup). Still fully supported for existing consumers. New repos should use `pr-review.yml` instead. |
| `workflows/sarif-codeql.yml` | GitHub Actions: example **CodeQL + AI review** pipeline using Capability B (SARIF 2.1.0 ingestion) |
| `pipelines/bitbucket-pipelines.yml` | Bitbucket Pipelines: automatic review on PR open/update |
| `pipelines/.gitlab-ci.yml` | GitLab CI: automatic review on MR open/update |

GitHub workflows use the `container-action` variant, which pulls a pinned public image from GHCR with all analyzer binaries pre-installed. Bitbucket and GitLab pipelines use the same image directly.

### Standardized call pattern

`workflows/pr-review.yml` is the reference template. Every input reads from a repo variable with a safe default fallback:

```yaml
image-tag: ${{ vars.AI_REVIEW_IMAGE_TAG || 'latest' }}
engine: ${{ vars.AI_PR_REVIEW_ENGINE || 'python' }}
context-enrichment: ${{ vars.AI_REVIEW_CONTEXT_ENRICHMENT || 'false' }}
# ... etc.
```

Once the workflow is in place, you can change behavior at any time by setting (or unsetting) the corresponding variable in **Settings → Secrets and variables → Actions → Variables** — no further PR to the workflow file needed.

See [Configuration → Repository variables](../docs/configuration.md#repository-variables) for the full list.

## Setup

### 1. Add your LLM API key

Add your provider API key as a repository secret named `AI_REVIEW_API_KEY` (or `ANTHROPIC_API_KEY` — both work). For other providers, substitute the appropriate secret name and update `provider` in the workflow.

### 2. Copy the unified workflow file

Download the single file into your repo:

```bash
mkdir -p .github/workflows

curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/pr-review.yml \
  -o .github/workflows/ai-pr-review.yml
```

Or, if you have the repo cloned locally:

```bash
mkdir -p .github/workflows
cp /path/to/ai-pr-review/examples/workflows/pr-review.yml .github/workflows/ai-pr-review.yml
```

Commit and push. The PR review fires automatically on the next opened or updated PR. Slash commands become active once this file is merged to your default branch (GitHub runs `issue_comment` and `pull_request_review_comment` workflows from the default branch only).

#### Two-file setup (existing consumers)

If you already have a separate `ai-pr-review-commands.yml` from an earlier setup, it continues to work unchanged — no migration is required. The reusable workflow it calls has not changed. If you want to consolidate to a single file, copy `pr-review.yml` over your existing `ai-pr-review.yml` and delete `ai-pr-review-commands.yml`.

To add the two-file setup from scratch (advanced use case or custom command wiring):

```bash
mkdir -p .github/workflows

curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/pr-review.yml \
  -o .github/workflows/ai-pr-review.yml

curl -fsSL \
  https://raw.githubusercontent.com/tag1consulting/ai-pr-review/main/examples/workflows/comment-triggers.yml \
  -o .github/workflows/ai-pr-review-commands.yml
```

## Auto-full for release PRs

The example workflow auto-selects full review mode when the source branch starts with `release/`. Customize the `startsWith()` pattern in the `review-mode` expression for your repo's branch convention. See [README > Auto-detecting release PRs](../README.md#auto-detecting-release-prs) for common patterns.

## Slash commands

Slash commands are built into `pr-review.yml` — no additional file needed. The `slash-commands` job in that file calls a [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows) hosted in this repository. All command parsing, review dispatch, and dismiss logic lives upstream — consumers don't need to maintain it.

Once `pr-review.yml` is merged to your default branch, post these commands in any PR comment:

| Command | Effect |
|---|---|
| `/ai-pr-review rescan` | Force full-diff re-review of the PR |
| `/ai-pr-review review-full` | Run all agents (full mode) |
| `/ai-pr-review skip` | Add `skip-ai-review` label to suppress the next review |
| `/ai-pr-review help` | Post the command list as a reply |
| `/ai-pr-review dismiss` | Reply to an inline review comment to mark that thread a false positive (dispatched via `pull_request_review_comment`, not `issue_comment`) |
| `/ai-pr-review false-positive [reason]` | **Learning loop.** Persist a false-positive verdict to the learning store. Post as a reply on the AI's inline finding (recommended — also resolves the thread on success) **or** as a top-level PR comment. Requires `AI_REVIEW_FEEDBACK_LOOP=true`. OWNER/MEMBER only. |
| `/ai-pr-review wont-fix [reason]` | **Learning loop.** Persist a "won't fix / by design" verdict. Same posting rules as `false-positive` (review-thread reply preferred). |
| `/ai-pr-review feedback <text>` | **Learning loop.** Persist free-form feedback for future review runs. |
| `/ai-pr-review explain` | **Learning loop.** Request a longer explanation (stub for now). |
| `/ai-pr-review revise <hint>` | **Learning loop.** Request agent revision with a hint (stub for now). |

The basic commands (`rescan`, `review-full`, `skip`, `dismiss`, `help`) allow OWNER, MEMBER, or COLLABORATOR. The learning-loop commands are restricted to OWNER and MEMBER only because they persist data that influences every future review repo-wide. Both restrictions are enforced via `author_association` guards in the workflow — GitHub does **not** do this automatically.

See [docs/slash-commands.md](../docs/slash-commands.md) for full details.

## Important: default-branch dispatch

Both `issue_comment` and `pull_request_review_comment` workflows run from the **default branch** of your repository, not from the PR branch. This means:

- Slash commands only work **after `ai-pr-review.yml` is merged** to your default branch.
- If you introduce `ai-pr-review.yml` in a PR, the automatic review (`pull_request` trigger) starts working immediately, but slash commands won't respond until that PR merges to the default branch.

This is a GitHub Actions platform behavior, not a limitation of this action.

## Opt-in capabilities

Three optional features can be enabled independently. All default off, all require the Python engine (the default since v1.0.0). Set the corresponding repo variable to turn them on without editing the workflow.

| Variable | Default | Effect |
|----------|---------|--------|
| `AI_REVIEW_CONTEXT_ENRICHMENT` | `false` | Tree-sitter symbol-context injection into agent prompts (`<symbol-context>` block) |
| `AI_REVIEW_SARIF_PATHS` | `''` | Comma-separated SARIF 2.1.0 file paths to merge as findings — see [`workflows/sarif-codeql.yml`](workflows/sarif-codeql.yml) for the CodeQL flavor |
| `AI_REVIEW_FEEDBACK_LOOP` | `false` | Learning loop — persists `/ai-pr-review false-positive\|wont-fix\|feedback` verdicts and re-injects them into future reviews. GitHub-only. |
| `AI_EXCLUDE_PATTERNS` | `''` | Comma-separated git pathspec globs to exclude from the diff (e.g. `vendor/*,generated/*`). Appended to built-in excludes by default. (v1.1.0) |
| `AI_EXCLUDE_PATTERNS_MODE` | `append` | `append` adds to built-in vendor/lockfile excludes; `replace` discards them and uses only the patterns you supply. (v1.1.0) |
| `AI_REVIEW_ANALYZER_DIFF_SCOPE` | `cap` | How out-of-diff native-analyzer findings are handled. `cap` (default): downgrade to Low and collapse under `<details>`. `drop`: remove entirely. `off`: pass through unchanged. LLM-agent findings unaffected. (v1.2.0) |

See [Configuration → Opt-in capabilities](../docs/configuration.md#opt-in-capabilities) for the full env-var reference (retention knobs, token budgets, branch name).

## Using a pinned image version

For reproducible builds, pin the image tag instead of using `latest`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main
  with:
    image-tag: '0.8.0'   # pin to a specific release
    ...
```

Or set the `AI_REVIEW_IMAGE_TAG` repo variable to your chosen tag — the canonical `workflows/pr-review.yml` template reads it via `${{ vars.AI_REVIEW_IMAGE_TAG || 'latest' }}`.

Available tags: `latest`, `dev`, `<major>` (e.g. `0`), `<major.minor>` (e.g. `0.8`), `<major.minor.patch>` (e.g. `0.8.0`).

## Using the non-container action

To skip Docker entirely, use the root composite action instead. It installs shellcheck on the Actions runner but does not install the other analyzer binaries:

```yaml
- uses: tag1consulting/ai-pr-review@main
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ github.token }}
    pr-number: ${{ github.event.pull_request.number }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
```
