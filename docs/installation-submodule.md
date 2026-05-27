---
layout: default
title: Git Submodule
parent: Getting Started
nav_order: 5
render_with_liquid: false
---

# Installation: git submodule

Use this installation method if you need explicit, auditable version pinning
and prefer to commit the exact action source into your repository rather than
referencing a tag or container image.

For most new installs, prefer the
[container action](getting-started) — it is simpler to set up and
ships all analyzer binaries pre-installed.

## Add the submodule

```bash
mkdir -p .github/actions
git submodule add git@github.com:tag1consulting/ai-pr-review.git .github/actions/ai-pr-review
git commit -m "Add ai-pr-review submodule"
```

## Create the workflow

The submodule approach uses a **3-job pattern**
(`prepare` → `review` → `cleanup-rescan-label`) that isolates the PAT used
for submodule checkout from the job that executes the composite action. This
prevents the PAT from landing in the review job's `.git/config` where the
action's shell scripts could read it.

Create `.github/workflows/ai-review.yml` in your repository:

```yaml
name: AI PR Review

on:
  pull_request:
    types: [opened, synchronize, ready_for_review, labeled]

# Fork PRs cannot access secrets anyway; per-job guards below prevent
# untrusted fork branches from triggering jobs with write permissions.
permissions:
  contents: read
  pull-requests: write
  # issues: write is declared only on cleanup-rescan-label (least-privilege)

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  # Job 1: validate config and check out the repo (including the private
  # submodule) using the PAT. Credentials are scrubbed from .git/config
  # before uploading the workspace artifact so the PAT does not travel
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
      # Fail fast with a clear error when required config is missing, rather
      # than a cryptic git auth or API failure deeper in the pipeline.
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
      # tag1consulting/ai-pr-review) to clone the private submodule.
      # GITHUB_TOKEN cannot cross-repo clone private submodules.
      - name: Checkout with submodules
        uses: actions/checkout@v4
        with:
          submodules: true
          token: ${{ secrets.AI_PR_REVIEW_TOKEN }}
          fetch-depth: 0

      # actions/checkout writes the PAT into .git/config as a persistent
      # credential. Scrubbing it before upload prevents the PAT from
      # travelling to the review job via the artifact.
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
  # PAT is never in its git config or accessible to the action.
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

      # actions/upload-artifact strips file permissions. Restore execute
      # bits on the composite action's shell scripts only — NOT on
      # .git/hooks/*, which would allow arbitrary code execution via a
      # malicious hook committed to the PR branch.
      - name: Restore executable bits
        run: find .github/actions/ai-pr-review -name "*.sh" -exec chmod +x {} +

      - uses: ./.github/actions/ai-pr-review
        env:
          FORCE_FULL_DIFF: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-rescan') }}
        with:
          provider: ${{ vars.AI_REVIEW_PROVIDER || 'anthropic' }}
          api-key: ${{ secrets.AI_REVIEW_API_KEY }}
          base-url: ${{ vars.AI_REVIEW_BASE_URL || '' }}
          review-mode: ${{ contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' || 'quick' }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}

  # Job 3: always attempt to remove the ai-review-rescan label.
  # needs: [prepare, review] with if: always() ensures this runs even when
  # the review job is cancelled (e.g. by the concurrency rule on a new push)
  # and when prepare is skipped (e.g. fork PRs).
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

> **Why 3 jobs?** `actions/checkout` with a PAT writes the token into
> `.git/config` as a persistent credential readable by any subsequent step in
> the same job. Isolating checkout into its own job (and scrubbing credentials
> before uploading the workspace artifact) keeps the PAT out of the job that
> executes third-party shell scripts.

## Configure secrets and variables

In the **consuming** repository's settings:

**Secrets:**
- `AI_REVIEW_API_KEY` — API key for your chosen LLM provider
- `AI_PR_REVIEW_TOKEN` — GitHub PAT with `repo` scope (or fine-grained read
  access to `tag1consulting/ai-pr-review`) for submodule checkout.
  `GITHUB_TOKEN` cannot cross-repo clone private submodules.

**Variables** (optional):
- `AI_REVIEW_PROVIDER` — Provider name (default: `anthropic`)
- `AI_REVIEW_BASE_URL` — Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`)
- `AI_REVIEW_MODEL_STANDARD` — Override the standard model ID
- `AI_REVIEW_MODEL_PREMIUM` — Override the premium model ID (full mode only)

## Updating the submodule pin

```bash
cd .github/actions/ai-pr-review
git fetch --all
git checkout v0.11.0
cd ../../..
git add .github/actions/ai-pr-review
git commit -m "Bump ai-pr-review submodule to v0.11.0"
```
