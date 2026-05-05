---
layout: default
title: Static Analyzers
nav_order: 5
---

# Static Analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path and fall back to sequential when `parallel: false`. If a binary is missing, the wrapper script emits a WARNING to stderr and returns an empty findings array — the review is never blocked.

The container action ships all analyzer binaries pre-installed. For the direct-action or submodule paths, install the binaries you need; see [runtime dependencies](installation-direct-action#runtime-dependencies).

| Analyzer | Language gate | Severity mapping | Confidence | Source tag |
|----------|--------------|-----------------|------------|------------|
| **shellcheck** | `.sh`, `.bash` | `error`→High, `warning`→Medium | 95 | `shellcheck` |
| **semgrep** | Any file | `ERROR`→High, `WARNING`→Medium, else→Low | 90 | `semgrep` |
| **trufflehog** | Any file | Verified secret→Critical, Unverified→High | 95 / 85 | `trufflehog` |
| **ruff** | `.py` files | `F`/`E` prefix→High, `W`/`C`→Medium, else→Low | 90 | `ruff` |
| **golangci-lint** | `.go` files | `errcheck`/`govet`/`staticcheck`→High, others→Medium | 90 | `golangci-lint` |
| **hadolint** | `Dockerfile*`, `*.dockerfile` | `error`→High, `warning`→Medium, else→Low | 90 | `hadolint` |
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | All→Medium (checkov has no per-check severity) | 80 | `checkov` |
| **phpcs** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | `ERROR`→High, `WARNING`→Medium; Drupal+DrupalPractice standard when available, else PSR12 | 90 | `phpcs` |
| **eslint** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | severity 2→High, severity 1→Medium; uses consumer's config — no-op if no `eslint.config.*` or `.eslintrc.*` found | 90 | `eslint` |
| **phpstan** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | All findings→High; runs at level `PHPSTAN_LEVEL` (default 3) unless consumer has `phpstan.neon`/`phpstan.neon.dist` | 85 | `phpstan` |
| **kube-linter** | `.yaml`, `.yml`, `.json` with `apiVersion:` + `kind:` headers | All findings→Medium (reliability-focused: missing probes, resource limits, etc.) | 85 | `kube-linter` |
| **tflint** | `.tf`, `.tfvars` | `error`→High, `warning`→Medium, `notice`→Low; runs per Terraform module directory | 90 | `tflint` |

## Dependency vulnerability check

When a PR modifies a supported dependency manifest, the action queries [OSV.dev](https://osv.dev/) for known vulnerabilities affecting the declared versions and surfaces them as findings alongside the LLM review.

| Manifest | Ecosystem |
|----------|-----------|
| `go.mod` | Go |
| `package.json` | npm |
| `requirements.txt` | PyPI |
| `composer.json` | Packagist |

Findings are mapped from CVSS score: >= 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, below 4.0 or unscored → Low. Critical and High findings trigger `REQUEST_CHANGES` on the PR review just like any other high-severity finding.

No configuration is required — the check runs automatically when a manifest file is in the diff. The OSV.dev API is unauthenticated and free. If the API is unreachable, the check emits a warning and continues — the review is never blocked by CVE-lookup failures.

To accept a specific CVE (e.g. library used only in a test fixture), add a suppression rule matching the CVE or GHSA ID:

```json
{
  "id": "accept-risk-CVE-2025-12345",
  "reason": "Library used only in test fixtures, not production",
  "match": {
    "pattern": "CVE-2025-12345|GHSA-xxxx-yyyy-zzzz"
  }
}
```

See [Suppression rules](suppression) for the full suppression schema.
