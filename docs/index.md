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
    <p>Up to 9 specialized AI agents analyze your code from different perspectives: architecture, security, edge cases, and more.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> 18 Static Analyzers</h3>
    <p>Shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, and tflint ship as binaries in the container image. ESLint runs only when you install it. cve-check, dep-exists, suppression-check (comments that turn off a security rule), and three documentation analyzers (docs-api-check, docs-ref-check, and docs-drift-check) also run. All 18 run as native Python.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> Works Everywhere</h3>
    <p>GitHub Actions, Bitbucket Cloud Pipelines, and GitLab CI/CD. Anthropic, OpenAI, Google, and Bedrock proxy providers.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> One-Click Fixes</h3>
    <p>Code suggestion buttons let PR and MR authors accept fixes with one click. They use the suggestion block syntax of GitHub and GitLab.</p>
  </div>
</div>

## What it does

On every push to a pull request, AI PR Review runs a roster of LLM agents and deterministic static analyzers against the diff, then posts a structured review. The review has a summary comment and inline findings with "Apply suggestion" buttons where they apply. The review is incremental: later pushes review only what changed. A JSON rules file suppresses known false positives. The tool continues without error if a model times out or a scanner is missing. It runs on GitHub Actions, Bitbucket Cloud Pipelines, or GitLab CI/CD. It works with Anthropic, OpenAI, Google, or any OpenAI-compatible endpoint.

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

Reviews start on the next PR.

## What's new in v2.25.0

**Fork pull requests are reviewed automatically (behavior change).** `AI_FORK_POLICY` now defaults to `auto`. A fork pull request gets a review at once, with a scrubbed environment, fewer analyzers, no PR body in the prompts, a COMMENT-only output and spend caps. Set `AI_FORK_POLICY=label` to require a maintainer approval, or `block` to switch fork review off. See [Fork pull requests](fork-prs).

**Upgrade warnings.** An agent that returns unreadable output now counts as a failed agent on every run, so the review posts COMMENT and not APPROVE. With `AI_FAIL_ON_FINDINGS=true` it also exits with code 2. A custom GitHub bot identity must set `GITHUB_BOT_USERNAME`. The checkout step must set `persist-credentials: false` for a fork review. A proxy value with credentials does not reach analyzers. The `@mention` neutralizing applies to fork runs only.

**A review says when a resolved finding is back.** If a later run finds a similar finding where this review resolved one, the comment says so. GitHub only.

**Telemetry schema 4.** `agent_yield` records what each finding agent returned.

See [Version History → v2.25.0](version-history/v2.25.0) for details.

## What's new in v2.24.1

**Security: the pull request checkout can no longer shadow the engine's Python modules.** A file in a pull request, such as `ai_pr_review/__main__.py` or `yaml.py`, could run instead of the installed module and see the review's API keys. The image and the actions now set `PYTHONSAFEPATH=1`. Update `image-tag` to `2.24.1` or leave it at `latest`. If you run the engine by hand, set `PYTHONSAFEPATH=1` first.

See [Version History → v2.24.1](version-history/v2.24.1) for details.

## What's new in v2.24.0

**A provider "prompt too long" rejection skips the review.** Before, the provider rejected the prompt, every agent failed, and the comment said "2 failed agents". Now the review stops and says which agent does not fit. Use `exclude-patterns` for generated files.

**Behavior change: the severity icons are different shapes.** Critical is ⛔, High is 🚨, Medium is 🔶, and Low is 💬. 🚨 meant Critical before. An old comment keeps its old icon until the next review rewrites it.

**Bitbucket: the `feedback` command.** `/ai-pr-review feedback <text>` saves a note to the learning loop, with the same authorization as the verdict commands.

**Behavior change: a style finding no longer merges into a nearby security finding.** A missing docblock used to absorb a security finding on a nearby line and show it as "corroborated". Now style and documentation findings stay separate, phpcs style sniffs are Low, and fewer findings show as corroborated.

**A new analyzer, `suppression-check`.** It flags a comment that turns off a security rule, such as `# noqa: S301` or `# nosec`. Add it to an `analyzers` allowlist if you use one.

See [Version History → v2.24.0](version-history/v2.24.0) for details.

## Learn more

**Start here**

- [Getting started](getting-started): Installation, requirements, secrets and variables
- [Configuration](configuration): Action inputs and LLM provider options

**Optional capabilities:** Three independent features. They are off by default, except context enrichment, which is on in the container action. They use the Python engine (the default since v1.0.0):

- [Tree-sitter context enrichment](configuration#opt-in-capabilities): Adds the symbol definitions that the diff references to the agent prompts. This reduces invented "should check X" findings.
- [SARIF 2.1.0 ingestion](static-analyzers#sarif-ingestion): Merges findings from external scanners (CodeQL, Semgrep, Trivy, Bandit) into the same dedup and post pipeline as native analyzers.
- [Learning loop](learning-loop): Reviewers post `/ai-pr-review false-positive | wont-fix | feedback` to save verdicts to a dedicated git branch. Later reviews see them as a `<repo-feedback>` block.

**Reference**

- [Features](features): Code suggestions, incremental reviews, resilience, token usage
- [Version History](version-history): What changed in each release
- [Agents & profiles](agents): Review agents, severity icons, review modes, language profiles
- [Static analyzers](static-analyzers): Analyzer table, dependency vulnerability check, SARIF ingestion
- [Suppression rules](suppression): Suppress false positives with JSON rules. Scope a rule to a line range with `match.line_start` and `match.line_end` (v1.1.0)
- [Diff-scope severity cap](configuration#static-analyzer-options): Use `analyzer-diff-scope` to control how the tool handles out-of-diff native-analyzer findings (v1.2.0)
- [Fork pull requests](fork-prs): How the engine reviews a pull request from a fork, what it limits, and the spend caps
- [Slash commands](slash-commands): PR-comment commands (rescan, review-full, skip, dismiss, help, plus learning-loop commands)

**Internals**

- [Architecture](architecture): Directory tree, data flow, dependencies
- [Local development](local-development): Run the container locally against any PR

**Contributing**

- [Contributing guide](https://github.com/tag1consulting/ai-pr-review/blob/main/CONTRIBUTING.md): Step-by-step recipes for adding analyzers, agents, language profiles, and VCS providers
- [Internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/architecture-internals.md): Deep implementation details for maintainers
