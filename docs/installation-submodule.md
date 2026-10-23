---
layout: default
title: Git Submodule
parent: Configuration
nav_order: 7
render_with_liquid: false
---

# Installation: git submodule

Use this installation method to pin an exact, auditable version. It commits the action source into your repository. You do not reference a tag or a container image.

For most new installs, use the [container action](getting-started). It is simpler to set up and ships the analyzer binaries pre-installed.

## Add the submodule

The `tag1consulting/ai-pr-review` repository is public. Add it with the HTTPS URL:

```bash
mkdir -p .github/actions
git submodule add https://github.com/tag1consulting/ai-pr-review.git .github/actions/ai-pr-review
git commit -m "Add ai-pr-review submodule"
```

## Create the workflow

Create `.github/workflows/ai-review.yml` in your repository. Because the repository is public, the default `GITHUB_TOKEN` can check out the submodule. You do not need a PAT.

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, labeled]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.event.pull_request.draft == false &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review') &&
      (github.event.action != 'labeled' ||
       github.event.label.name == 'ai-review-full' ||
       github.event.label.name == 'ai-review-rescan')
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
          fetch-depth: 0

      - uses: ./.github/actions/ai-pr-review
        env:
          FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
        with:
          review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || '' }}
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          fail-on-findings: ${{ vars.AI_REVIEW_FAIL_ON_FINDINGS || 'true' }}
```

The next section shows a 3-job variant. Use it only if your submodule points to a private fork or mirror.

## Variant: private fork or mirror

The default `GITHUB_TOKEN` cannot clone a private submodule from another repository. For a private fork or mirror, use a PAT for the submodule checkout. Use a 3-job pattern (`prepare`, `review`, `cleanup-rescan-label`) to isolate that PAT from the job that runs the composite action. This keeps the PAT out of the `.git/config` of the review job, where the action code could read it.

Create `.github/workflows/ai-review.yml` in your repository:

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, labeled]

# Fork PRs cannot access secrets anyway. The per-job guards below stop
# untrusted fork branches from starting jobs with write permissions.
permissions:
  contents: read
  pull-requests: write
  # issues: write is declared only on cleanup-rescan-label (least-privilege)

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  # Job 1: validate config and check out the repo (including the private
  # submodule) with the PAT. Remove the credentials from .git/config
  # before the workspace artifact upload, so the PAT does not go
  # to the review job.
  prepare:
    runs-on: ubuntu-latest
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.event.pull_request.draft == false &&
      github.actor != 'dependabot[bot]' &&
      github.actor != 'renovate[bot]' &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review') &&
      (github.event.action != 'labeled' ||
       github.event.label.name == 'ai-review-full' ||
       github.event.label.name == 'ai-review-rescan')
    steps:
      # Fail early with a clear error when required config is missing.
      # This avoids an unclear git auth or API failure later in the pipeline.
      - name: Validate required configuration
        env:
          AI_REVIEW_API_KEY: ${{ secrets.AI_REVIEW_API_KEY }}
          AI_PR_REVIEW_TOKEN: ${{ secrets.AI_PR_REVIEW_TOKEN }}
        run: |
          missing=()
          [ -z "$AI_REVIEW_API_KEY" ] && missing+=("secret AI_REVIEW_API_KEY")
          [ -z "$AI_PR_REVIEW_TOKEN" ] && missing+=("secret AI_PR_REVIEW_TOKEN")
          if [ ${#missing[@]} -gt 0 ]; then
            echo "ERROR: Missing required configuration: ${missing[*]}"
            echo "See the README for setup instructions."
            exit 1
          fi

      # AI_PR_REVIEW_TOKEN needs repo scope (or fine-grained read access to
      # your private fork) to clone the private submodule.
      # GITHUB_TOKEN cannot clone private submodules from other repositories.
      - name: Checkout with submodules
        uses: actions/checkout@v4
        with:
          submodules: true
          token: ${{ secrets.AI_PR_REVIEW_TOKEN }}
          fetch-depth: 0

      # actions/checkout writes the PAT into .git/config as a persistent
      # credential. Remove it before the upload, so the PAT does not
      # go to the review job in the artifact.
      - name: Scrub git credentials before artifact upload
        run: git config --unset-all http.https://github.com/.extraheader || true

      - name: Upload workspace
        uses: actions/upload-artifact@v4
        with:
          name: workspace
          path: .
          include-hidden-files: true
          retention-days: 1

  # Job 2: run the composite action against the downloaded workspace.
  # This job does NOT run actions/checkout with the PAT, so the repo-scoped
  # PAT is never in its git config and the action cannot read it.
  review:
    needs: prepare
    runs-on: ubuntu-latest
    if: github.event.pull_request.head.repo.full_name == github.repository
    steps:
      - name: Download workspace
        uses: actions/download-artifact@v4
        with:
          name: workspace
          path: .

      - uses: ./.github/actions/ai-pr-review
        env:
          FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          model-standard: ${{ vars.AI_REVIEW_MODEL_STANDARD || '' }}
          model-premium: ${{ vars.AI_REVIEW_MODEL_PREMIUM || '' }}
          review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || '' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          fail-on-findings: ${{ vars.AI_REVIEW_FAIL_ON_FINDINGS || 'true' }}

  # Job 3: always try to remove the ai-review-rescan label.
  # needs: [prepare, review] with if: always() makes this job run even when
  # the review job is cancelled (for example, by the concurrency rule on a new push)
  # and when prepare is skipped (for example, fork PRs).
  cleanup-rescan-label:
    needs: [prepare, review]
    if: >-
      always() &&
      github.event.pull_request.head.repo.full_name == github.repository &&
      !contains(github.event.pull_request.labels.*.name, 'skip-ai-review')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
    steps:
      - name: Remove ai-review-rescan label
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh api \
            --method DELETE \
            repos/${{ github.repository }}/issues/${{ github.event.pull_request.number }}/labels/ai-review-rescan \
            || true
```

> **Why 3 jobs?** `actions/checkout` with a PAT writes the token into `.git/config` as a persistent credential. Any later step in the same job can read it. Put the checkout in its own job and remove the credentials before the workspace artifact upload. This keeps the PAT out of the job that runs the action code.

## Configure secrets and variables

In the **consuming** repository's settings:

**Secrets:**
- `AI_REVIEW_API_KEY`: API key for your chosen LLM provider
- `AI_PR_REVIEW_TOKEN` (private fork or mirror only): GitHub PAT with `repo` scope (or fine-grained read access to your fork) for the submodule checkout. `GITHUB_TOKEN` cannot clone a private submodule from another repository.

**Variables** (optional):
- `AI_REVIEW_PROVIDER`: Provider name (default: `anthropic`)
- `AI_REVIEW_BASE_URL`: Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`)
- `AI_REVIEW_MODEL_STANDARD`: Override the standard model ID
- `AI_REVIEW_MODEL_PREMIUM`: Override the premium model ID (full mode only)
- `AI_REVIEW_FAIL_ON_FINDINGS`: Exit with code 2 when the review outcome is `REQUEST_CHANGES` or `COMMENT` (incomplete or unknown risk). This fails the workflow step. The default is `true` in the workflows above. The `fail-on-findings` input in `action.yml` defaults to `'false'`, so the workflows set it explicitly.

## Updating the submodule pin

```bash
cd .github/actions/ai-pr-review
git fetch --all
git checkout v2.25.0
cd ../../..
git add .github/actions/ai-pr-review
git commit -m "Bump ai-pr-review submodule to v2.25.0"
```
