---
layout: home
title: Home
nav_exclude: true
permalink: /
render_with_liquid: false
hero_title: AI PR Review
hero_tagline: "AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline findings directly on your PRs."
---

<div class="features">
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> Multi-Agent Review</h3>
    <p>Up to 8 specialized AI agents analyze your code from different perspectives — architecture, security, edge cases, and more.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> 13 Static Analyzers</h3>
    <p>Shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, eslint, phpstan, kube-linter, and tflint ship as binaries in the container image; cve-check runs as pure Python (OSV.dev HTTP queries, no external binary). All 13 run as native Python.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> Works Everywhere</h3>
    <p>GitHub Actions, Bitbucket Cloud Pipelines, and GitLab CI/CD. Anthropic, OpenAI, Google, and Bedrock proxy providers.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> One-Click Fixes</h3>
    <p>Code suggestion buttons let PR/MR authors accept fixes with a single click, powered by GitHub and GitLab's suggestion block syntax.</p>
  </div>
</div>

## What it does

On every push to a pull request, AI PR Review runs a roster of LLM agents and deterministic static analyzers against the diff, then posts a structured review — a summary comment plus inline findings with "Apply suggestion" buttons where applicable. It's incremental (subsequent pushes only review what changed), suppresses known false positives via a JSON rules file, and is designed to fail gracefully when a model times out or a scanner is missing. Runs on GitHub Actions, Bitbucket Cloud Pipelines, or GitLab CI/CD against Anthropic, OpenAI, Google, or any OpenAI-compatible endpoint.

## Quick start

Get AI reviews on your PRs in two steps:

**1. Add your LLM API key** as a repository secret named `ANTHROPIC_API_KEY` (or the equivalent for your [provider](configuration)).

**2. Create `.github/workflows/ai-review.yml`** with this minimal workflow:

```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          
      - uses: tag1consulting/ai-pr-review/container-action@main
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

That's it — reviews start firing on the next PR.

## What's new in v2.13.2

**Two real bugs, both found live while validating the prior two releases, are fixed, alongside a full documentation-accuracy pass.** A compute-phase skip ("no changed files" or diff-too-large) crashed instead of posting cleanly whenever `AI_REVIEW_MODE` was left at its unset-sentinel empty string — reachable whenever a review workflow runs against an already-merged PR (issue #927). On Bitbucket, `post_findings` could fail with "no summary comment to attach findings to" on a brand-new (first-ever) summary comment, even though it had just been created successfully — a fresh bot identity's first-ever post to a PR reliably reproduces this (issue #930). Separately, a thorough re-check of every doc against current code found and corrected a batch of stale claims accumulated since v2.12.x — env var defaults, analyzer counts, provider capability descriptions, and two example workflows missing a fail-closed default.

See [Version History → v2.13.2](version-history/v2.13.2) for details.

## What's new in v2.13.1

**Two v2.13.0 release-doc gaps, found by that release's own AI-review response loop, are fixed.** The `#approval-ceiling` configuration reference said GitLab *and* Bitbucket "never post a real approval state", stale as of issue #918. Issue #918's Bitbucket default-on real-approval behavior is now flagged as a **Behavior change** with upgrade guidance. Docs-only, no code changed.

See [Version History → v2.13.1](version-history/v2.13.1) for details.

## What's new in v2.13.0

**Bitbucket reaches finding-lifecycle parity with GitHub/GitLab, and five security findings surfaced during that work are fixed.** Bitbucket now dedups findings across runs and renders them inline as Code Insights annotations (both on by default), and polls PR comments for `/ai-pr-review dismiss|false-positive|wont-fix|fixed` commands at the start of the next run, opt-in via `AI_BITBUCKET_VERDICTS` — the closest a comment-triggerless CI can get to GitHub/GitLab's immediate slash-command handling (issues #839, #873, #874). A follow-up pass against a real Bitbucket Pipelines run found and fixed three more gaps: annotated findings were invisible in the review comment itself (#920), the starter pipeline never failed the build on Critical/High findings (#917), and the decided review outcome had no real effect on the PR's own reviewer-state UI until now (#918, capped by `approval-ceiling` the same as GitHub/GitLab). The same body of work's security-review passes surfaced a verdicts-marker forgery path via unsanitized LLM narrative output, closed in two passes covering both the original gap and the id-map/acks/judge-map/`Finding.source` sibling channels (#886, #913), a bullet-syntax forgery redirecting dismiss commands (#914), a fingerprint delimiter collision (#887), and a Bitbucket comment-authorship gap (#894) — all fixed. Also new: `approval-ceiling`, letting a repo require a human to make every merge decision instead of the bot (issue #858).

See [Version History → v2.13.0](version-history/v2.13.0) for details.

## Learn more

**Start here**

- [Getting started](getting-started) — Installation, requirements, secrets and variables
- [Configuration](configuration) — Action inputs and LLM provider options

**Opt-in capabilities** — three independent features, all default off, all require the Python engine (the default since v1.0.0):

- [Tree-sitter context enrichment](configuration#opt-in-capabilities) — inject symbol definitions referenced in the diff into agent prompts; reduces hallucinated "should check X" findings
- [SARIF 2.1.0 ingestion](static-analyzers#sarif-ingestion) — merge findings from external scanners (CodeQL, Semgrep, Trivy, Bandit) into the same dedup/post pipeline as native analyzers
- [Learning loop](learning-loop) — reviewers post `/ai-pr-review false-positive | wont-fix | feedback` to persist verdicts to a dedicated git branch; future reviews see them as a `<repo-feedback>` block

**Reference**

- [Features](features) — Code suggestions, incremental reviews, resilience, token usage
- [Version History](version-history) — What changed in each release
- [Agents & profiles](agents) — Review agents, severity icons, review modes, language profiles
- [Static analyzers](static-analyzers) — Analyzer table, dependency vulnerability check, SARIF ingestion
- [Suppression rules](suppression) — Suppress false positives with JSON rules; scope rules to a line range with `match.line_start` / `match.line_end` (v1.1.0)
- [Diff-scope severity cap](configuration#static-analyzer-options) — control how out-of-diff native-analyzer findings are handled via `analyzer-diff-scope` (v1.2.0)
- [Slash commands](slash-commands) — PR-comment commands (rescan, review-full, skip, dismiss, help, plus learning-loop commands)

**Internals**

- [Architecture](architecture) — Directory tree, data flow, dependencies
- [Local development](local-development) — Run the container locally against any PR

**Contributing**

- [Contributing guide](https://github.com/tag1consulting/ai-pr-review/blob/main/CONTRIBUTING.md) — Step-by-step recipes for adding analyzers, agents, language profiles, and VCS providers
- [Internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/architecture-internals.md) — Deep implementation details for maintainers
