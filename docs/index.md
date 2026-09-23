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

## What's new in v2.13.0

**Bitbucket reaches finding-lifecycle parity with GitHub/GitLab, and three security findings surfaced during that work are fixed.** Bitbucket now dedups findings across runs, renders them inline as Code Insights annotations, and (opt-in) polls PR comments for `/ai-pr-review dismiss|false-positive|wont-fix|fixed` commands at the start of the next run — the closest a comment-triggerless CI can get to GitHub/GitLab's immediate slash-command handling (issues #839, #873, #874). The same body of work's security-review passes surfaced a verdicts-marker forgery path via unsanitized LLM narrative output (#886), a fingerprint delimiter collision (#887), and a Bitbucket comment-authorship gap (#894) — all fixed. Also new: `approval-ceiling`, letting a repo require a human to make every merge decision instead of the bot (issue #858).

See [Version History → v2.13.0](version-history/v2.13.0) for details.

## What's new in v2.12.0

**Review agents now see the PR's own title and description, not just the diff, and GitLab findings update in place instead of only being deduplicated.** A PR's stated intent ("this refactor deliberately changes X, not a regression") was previously invisible to every finding agent — a `<pr-context>` block now closes that gap for all except the deliberately diff-only `blind-hunter` (issue #813). On GitLab, a re-detected finding whose content changed now gets its existing discussion note updated in place, with a reply on severity escalation, closing the last gap between GitLab's cross-run dedup and GitHub's canonical-review reuse (issue #710). Three further capabilities land alongside these: per-agent output-token budget overrides (issue #191), a pre-flight LLM cost estimate with a configurable abort ceiling (issue #24), and judge-pass verdict/corroboration state now persisted to the feedback-learning store for future analysis (issue #841).

See [Version History → v2.12.0](version-history/v2.12.0) for details.

## What's new in v2.11.0

**GitLab stops silently dropping findings it can't anchor to an inline diff line, and two smaller correctness fixes land.** Out-of-diff findings (or ones bumped over the `max_inline` cap) were computed but never rendered anywhere on GitLab — GitHub and Bitbucket both already surface these — and are now appended as a bullet list in the MR's summary note (issue #711). `REVIEW_TARGET`'s case-sensitivity could silently skip the standalone-mode deprecation warning and merge-commit-filter behavior for mixed-case input like `REVIEW_TARGET=Standalone` (issue #629), now normalized once at the config boundary. A Semgrep false positive on the phpstan analyzer's `subprocess.run` call (issue #789) is fixed with the project's deterministic `suppressions.json` mechanism, after an inline `# nosemgrep` comment tried first turned out to be unreliable across multi-file Semgrep scans in CI.

See [Version History → v2.11.0](version-history/v2.11.0) for details.

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
