---
layout: default
title: Architecture
nav_order: 7
---

# Architecture

```
ai-pr-review/
├── action.yml              # GitHub Actions composite action definition
├── review.sh               # Main orchestrator: diff, manifest, agent calls, assembly
├── llm-call.sh             # Multi-provider LLM API wrapper (curl-based)
├── post-review.sh          # GitHub API posting: summary, review, thread management
├── post-review-bitbucket.sh # Bitbucket Cloud API posting: summary comment
├── post-review-gitlab.sh   # GitLab API posting: summary, inline discussions, approval
├── analyzers/              # Static analyzer wrapper scripts
│   ├── run-shellcheck.sh   # Shellcheck wrapper for shell script findings
│   ├── run-cve-check.sh    # OSV.dev vulnerability lookup for dependency manifests
│   ├── run-semgrep.sh      # Semgrep SAST wrapper (optional binary)
│   ├── run-trufflehog.sh   # Trufflehog secret scanning wrapper (optional binary)
│   ├── run-ruff.sh         # Ruff Python linter wrapper (optional binary)
│   ├── run-golangci-lint.sh # golangci-lint Go linter wrapper (optional binary)
│   ├── run-hadolint.sh     # Hadolint Dockerfile linter wrapper (optional binary)
│   ├── run-checkov.sh      # Checkov IaC scanner wrapper (optional binary)
│   ├── run-phpcs.sh        # PHP_CodeSniffer wrapper, Drupal+DrupalPractice standard
│   ├── run-eslint.sh       # ESLint JS/TS wrapper — uses consumer config, no-op if absent
│   ├── run-phpstan.sh      # PHPStan static analysis wrapper (optional binary)
│   ├── run-kube-linter.sh  # kube-linter Kubernetes manifest wrapper (optional binary)
│   └── run-tflint.sh       # tflint Terraform linter wrapper (optional binary)
├── config/                 # Configuration and data files
│   ├── model-pricing.json  # Per-model token pricing for cost estimation
│   └── suppressions.json   # Declarative false-positive suppression rules
├── prompts/                # System prompts for each review agent
│   ├── pr-summarizer.md
│   ├── code-reviewer.md
│   ├── silent-failure-hunter.md
│   ├── architecture-reviewer.md
│   ├── security-reviewer.md
│   ├── blind-hunter.md
│   ├── edge-case-hunter.md
│   └── adversarial-general.md
├── language-profiles/      # Per-language review context
│   ├── go.md
│   ├── python.md
│   ├── typescript.md
│   ├── php.md
│   ├── shell.md
│   ├── ruby.md
│   ├── rust.md
│   ├── java.md
│   └── c++.md
├── tests/                  # bats-core unit tests
│   ├── review_functions.bats
│   ├── extract_findings.bats
│   ├── is_test_file.bats
│   ├── configurable_inputs.bats
│   ├── dedup_filter.bats
│   ├── apply_suppressions.bats
│   ├── call_agent.bats
│   ├── llm_call_functions.bats
│   ├── parse_valid_lines.bats
│   ├── post_review_functions.bats
│   ├── post_review_bitbucket_functions.bats
│   ├── post_review_gitlab_functions.bats
│   ├── retry_curl.bats
│   ├── run_shellcheck.bats
│   ├── run_cve_check.bats
│   ├── run_semgrep.bats
│   ├── run_trufflehog.bats
│   ├── run_ruff.bats
│   ├── run_golangci_lint.bats
│   ├── run_hadolint.bats
│   ├── run_checkov.bats
│   ├── run_phpcs.bats
│   ├── run_eslint.bats
│   ├── run_phpstan.bats
│   ├── run_kube_linter.bats
│   ├── run_tflint.bats
│   ├── test_helper.bash
│   └── fixtures/
└── .github/workflows/
    ├── ai-review.yml       # Self-test: runs the action on its own PRs
    ├── lint.yml            # Shellcheck + bats test suite
    ├── pages.yml           # GitHub Pages documentation site build
    └── publish-image.yml   # Container image build, push, and signing
```

## Data flow

1. **review.sh** computes the diff, builds a file manifest, detects languages
2. For each agent, **review.sh** assembles a context message and calls **llm-call.sh**
3. **llm-call.sh** sends the prompt to the configured LLM provider via curl
4. **review.sh** extracts JSON findings from agent responses, deduplicates, applies suppressions
5. The **provider-specific post-review script** resolves stale threads, posts the summary and findings, advances the SHA watermark

## Dependencies

The action requires `bash`, `curl`, `jq`, `git`, and `gh` — all pre-installed on standard GitHub-hosted runners. `shellcheck` is installed automatically if not already present.

The **container action** (recommended) ships all static analyzer binaries pre-installed at pinned versions — no runner setup needed. The **direct action reference** and **git submodule** paths do not install analyzer binaries; see [runtime dependencies](installation-direct-action#runtime-dependencies) for the optional install-in-workflow snippet.
