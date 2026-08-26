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

When `agents` is set, `exclude-agents` is ignored (allowlist takes precedence). Existing gates still apply on top: a tier-2 agent in the allowlist still won't run in quick mode, and a conditionally-triggered agent still won't run if its trigger didn't fire. Excluding `pr-summarizer` suppresses the PR summary comment entirely. Unknown names are rejected with an error and a suggestion. See [configuration.md](configuration.md#analyzer-and-agent-selection) for the env-var equivalents.

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
- Routing by changed-file path, base branch, or head branch via a repo-local `.github/ai-pr-review/policy.yml` (see below)

### Routing review depth by branch or path

For anything beyond a single global quick/full choice — e.g. near-zero-cost review for content-only PRs, a full smoke-test review once when features land on a staging branch, quick review on feature branches otherwise — add `.github/ai-pr-review/policy.yml` to your repo. See **[Policies](policy.md)** for the full schema, the precedence chain (an explicit `ai-review-full` label or `review-mode` input always wins over policy routing), and worked examples including a release-branch escalation.

The `ai-review-full` label and `workflow_dispatch`'s `review_mode` input remain the way to force a one-off full review regardless of any policy file.

**Bitbucket Pipelines / GitLab CI** work the same way — `policy.yml` is engine-side, not GitHub-specific — as long as `BASE_REF`/`HEAD_REF` are exported (see the example pipelines in `examples/pipelines/`).

## Language profiles

The action auto-detects languages from file extensions and injects per-language context into agent prompts. Language profiles are markdown files in `language-profiles/`:

| Profile file | Covers |
|---|---|
| `go.md` | Go |
| `python.md` | Python |
| `javascript.md` | JavaScript |
| `typescript.md` | TypeScript |
| `php.md` | PHP / Drupal |
| `shell.md` | Shell / Bash |
| `ruby.md` | Ruby / Rails |
| `rust.md` | Rust |
| `java.md` | Java |
| `c++.md` | C and C++ |
| `terraform.md` | Terraform |
| `yaml.md` | YAML |
| `kotlin.md` | Kotlin |
| `swift.md` | Swift |
| `csharp.md` | C# |
| `scala.md` | Scala |
| `sql.md` | SQL |
| `lua.md` | Lua |
| `perl.md` | Perl |

To add a new language, create a `language-profiles/<language>.md` file. The filename (without extension) must match the lowercase language key returned by `detect_language()` in `ai_pr_review/languages.py` for the relevant file extensions.
