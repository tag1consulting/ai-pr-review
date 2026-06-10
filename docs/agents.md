---
layout: default
title: Agents & Profiles
nav_order: 4
render_with_liquid: false
---

# Agents & Profiles

On every PR push, this action:

1. Computes the diff (full on first run, incremental on subsequent pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Runs deterministic checks on changed files: shellcheck, CVE lookups ([OSV.dev](https://osv.dev/)), semgrep SAST, trufflehog secret scanning, ruff (Python), golangci-lint (Go), hadolint (Dockerfiles), checkov (Terraform/K8s/IaC), phpcs (PHP/Drupal), eslint (JS/TS), phpstan (PHP static analysis), kube-linter (Kubernetes), and tflint (Terraform)
5. Posts a summary comment (first run only) and a review with inline findings
6. Auto-resolves stale bot threads and dismisses superseded reviews

## Review agents

**Quick mode** (default) runs 1-2 finding agents plus a summary agent on first run:

| Agent | Purpose |
|-------|---------|
| **pr-summarizer** | Generates a walkthrough summary (first run only) |
| **code-reviewer** | Finds bugs, logic errors, and code quality issues |
| **silent-failure-hunter** | Detects swallowed errors and unsafe fallbacks (runs when error-handling patterns are detected) |

**Full mode** adds 5 more agents:

| Agent | Purpose |
|-------|---------|
| **architecture-reviewer** | Evaluates design patterns, coupling, and scalability |
| **security-reviewer** | Checks for injection, auth, crypto, and supply chain issues |
| **blind-hunter** | Context-free review (zero project knowledge, catches familiarity blindness) |
| **edge-case-hunter** | Traces every branching path for unhandled gaps |
| **adversarial-general** | Cynical adversarial review |

Full mode also runs **issue-linker** (GitHub-only, full mode): discovers related issues/PRs and assesses whether they are resolved by the current changes.

## Controlling which agents run

Use the `agents` (allowlist) and `exclude-agents` (denylist) inputs to control which agents run. Both accept a comma-separated list of the agent names above. Empty (default) means all eligible agents run.

```yaml
# Run only code-reviewer and security-reviewer:
agents: 'code-reviewer,security-reviewer'

# Run everything except edge-case-hunter and adversarial-general:
exclude-agents: 'edge-case-hunter,adversarial-general'
```

When `agents` is set, `exclude-agents` is ignored (allowlist takes precedence). Existing gates still apply on top: a tier-2 agent in the allowlist still won't run in quick mode, and a conditionally-triggered agent still won't run if its trigger didn't fire. Excluding `pr-summarizer` suppresses the PR summary comment entirely. Unknown names are rejected with an error and a suggestion. Requires `engine: python` (the default). See [configuration.md](configuration.md#analyzer-and-agent-selection) for the env-var equivalents.

## Severity icons

Findings use shape-distinct icons for accessibility:

| Icon | Severity | Review action |
|------|----------|---------------|
| ❌ | Critical | REQUEST_CHANGES |
| 🚨 | High | REQUEST_CHANGES |
| 🔶 | Medium | APPROVE (informational) |
| 💬 | Low | APPROVE (informational) |

## Review modes

**Quick mode** (default): Runs the code-reviewer and (conditionally) silent-failure-hunter. Fast and cheap — suitable for every push.

**Full mode**: Runs up to 8 agents — 6 always-on finding agents (code-reviewer, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general), plus silent-failure-hunter (conditional) and pr-summarizer on first run. Trigger full mode by:
- Adding the `ai-review-full` label to the PR
- Using `workflow_dispatch` with `review_mode: full`
- Setting the `review-mode` input to `full`
- Auto-detecting release PRs (see below)

### Auto-detecting release PRs

Full mode can be triggered automatically based on branch name, PR title, or other PR metadata by extending the `review-mode` expression in your workflow file. This keeps release PRs from getting only a quick review without requiring someone to remember to add a label.

The [example workflow](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) demonstrates auto-detecting `release/*` branches:

```yaml
review-mode: >-
  ${{
    contains(github.event.pull_request.labels.*.name, 'ai-review-full') && 'full' ||
    startsWith(github.event.pull_request.head.ref, 'release/') && 'full' ||
    'quick'
  }}
```

Customize the `startsWith()` pattern for your repo's branch convention. Common patterns:

| Convention | Expression |
|---|---|
| Branch prefix `release/` | `startsWith(github.event.pull_request.head.ref, 'release/')` |
| Branch prefix `hotfix/` | `startsWith(github.event.pull_request.head.ref, 'hotfix/')` |
| PR title starts with "Release" | `startsWith(github.event.pull_request.title, 'Release ')` |
| Merges to `main` from `release/*` | `github.event.pull_request.base.ref == 'main' && startsWith(github.event.pull_request.head.ref, 'release/')` |
| Multiple patterns | Chain with `\|\|` — each clause evaluates to `'full'` or falls through |

Branch matching is case-sensitive — `release/v1.0` matches but `Release/v1.0` does not.

**Bitbucket Pipelines** — use a shell conditional instead of GitHub Actions expressions:

```bash
if [[ "$BITBUCKET_PR_SOURCE_BRANCH" == release/* ]]; then
  export AI_REVIEW_MODE=full
fi
```

## Language profiles

The action auto-detects languages from file extensions and injects per-language context into agent prompts. Language profiles are markdown files in `language-profiles/`:

| Profile file | Covers |
|---|---|
| `go.md` | Go |
| `php.md` | PHP / Drupal |
| `python.md` | Python |
| `shell.md` | Shell / Bash |
| `typescript.md` | TypeScript / JavaScript |
| `ruby.md` | Ruby / Rails |
| `rust.md` | Rust |
| `java.md` | Java |
| `c++.md` | C and C++ |

To add a new language, create a `language-profiles/<language>.md` file. The filename (without extension) must match the lowercase language key returned by `detect_language()` in `lib/languages.sh` for the relevant file extensions.
