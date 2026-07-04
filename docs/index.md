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

## What's new in v2.3.0

**Slash-command dismiss orchestration ported from workflow-embedded bash to the Python engine.** `/ai-pr-review dismiss`, `false-positive`, and `wont-fix` now run through a tested Python module and CLI subcommands instead of ~1,100 lines of inline bash and GraphQL calls. User-facing command syntax is unchanged. Also fixes two dismiss bugs: out-of-diff findings that couldn't be located, and a dismiss PUT attempted against an already-resolved review.

See [Features → v2.3.0](features#whats-new-in-v230) for details.

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

## Learn more

**Start here**

- [Getting started](getting-started) — Installation, requirements, secrets and variables
- [Configuration](configuration) — Action inputs and LLM provider options

**Opt-in capabilities** — three independent features, all default off, all require the Python engine (the default since v1.0.0):

- [Tree-sitter context enrichment](configuration#opt-in-capabilities) — inject symbol definitions referenced in the diff into agent prompts; reduces hallucinated "should check X" findings
- [SARIF 2.1.0 ingestion](static-analyzers#sarif-ingestion-capability-b) — merge findings from external scanners (CodeQL, Semgrep, Trivy, Bandit) into the same dedup/post pipeline as native analyzers
- [Learning loop](learning-loop) — reviewers post `/ai-pr-review false-positive | wont-fix | feedback` to persist verdicts to a dedicated git branch; future reviews see them as a `<repo-feedback>` block

**Reference**

- [Features](features) — Code suggestions, incremental reviews, resilience, token usage
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
