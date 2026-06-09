---
layout: default
title: Static Analyzers
nav_order: 5
---

# Static Analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path and fall back to sequential when `parallel: false`. If a binary is missing, the native analyzer emits a WARNING to stderr and returns an empty findings array — the review is never blocked.

The container action ships all analyzer binaries pre-installed. For the direct-action or submodule paths, install the binaries you need; see [runtime dependencies](installation-direct-action#runtime-dependencies).

| Analyzer | Language gate | Severity mapping | Confidence | Source tag |
|----------|--------------|-----------------|------------|------------|
| **shellcheck** | `.sh`, `.bash` | `error`→High, `warning`→Medium | 95 | `shellcheck` |
| **semgrep** | Any file | `ERROR`→High, `WARNING`→Medium, else→Low | 90 | `semgrep` |
| **trufflehog** | Any file | Verified secret→Critical, Unverified→High | 95 / 85 | `trufflehog` |
| **ruff** | `.py` files | `F`/`E` prefix→High, `W`/`C`→Medium, else→Low | 90 | `ruff` |
| **golangci-lint** | `.go` files | `errcheck`/`govet`/`staticcheck`→High, others→Medium | 90 | `golangci-lint` |
| **hadolint** | `Dockerfile*`, `*.dockerfile` | `error`→High, `warning`→Medium, else→Low | 90 | `hadolint` |
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | `CKV2_*` and `CKV_SECRET_*`→High; all other checks→Medium | 80 | `checkov` |
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

Findings are mapped from CVSS score: >= 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, below 4.0 → Low. Unscored or unparseable CVEs map to **High** (fail-safe — the same behavior as the `run-cve-check.sh` `severity_label` function). Critical and High findings trigger `REQUEST_CHANGES` on the PR review just like any other high-severity finding.

No configuration is required — the check runs automatically when a manifest file is in the diff. The OSV.dev API is unauthenticated and free. If the API is unreachable, the check emits a warning and continues — the review is never blocked by CVE-lookup failures.

To accept a specific CVE (e.g. a library used only in a test fixture), add a suppression rule matching the CVE or GHSA ID. See [Suppression rules](suppression#suppressing-cve-findings) for the schema and a worked example.

## SARIF ingestion (Capability B)

In addition to the built-in analyzers, the Python engine can ingest SARIF 2.1.0 output from any external tool (CodeQL, Semgrep Pro, Trivy, Snyk, custom scanners).

### Setup

1. Run your SARIF-producing tool as a prior step (e.g. CodeQL, Trivy).
2. Pass the output path(s) via the `sarif-paths` input:

```yaml
- uses: tag1consulting/ai-pr-review@main
  with:
    sarif-paths: 'results/codeql.sarif,results/trivy.sarif'
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

See `examples/workflows/sarif-codeql.yml` for a complete CodeQL + AI review pipeline.

### Severity mapping

| SARIF level | AI review severity |
|------------|-------------------|
| `error` | High |
| `warning` | Medium |
| `note` | Low |
| `none` | Low |

### Behavior

- Source tag: `sarif:<driver.name>` (e.g. `sarif:CodeQL`, `sarif:trivy`).
- Default confidence: 90.
- Remediation text: taken from the rule's `help.text` field when present.
- File URI prefixes (`file:///`, `file://`) are stripped from location paths.
- Findings from SARIF files are merged into the same dedup/suppress pipeline as findings from native analyzers and LLM agents.
- Unreadable or malformed SARIF files emit a `WARNING` log and are skipped (fail-soft).

---

## Implementation reference

As of v1.4.0, all 13 analyzers are implemented as native Python functions in `ai_pr_review/analyzers/native/`. The `analyzers/bridge.py` dispatcher maps each tool name to its Python callable. Each analyzer invokes the tool binary directly via `subprocess.run` and parses the JSON output in Python.

The `analyzers/run-<tool>.sh` bash wrappers still exist for the deprecated bash engine. For the full binary flags, input handling, output-field mapping, and path normalization reference for those wrappers, see [Analyzer Bash Wrapper Inventory](analyzers-bash-inventory).

**Note:** the `<TOOL>_MOCK_FILE` env vars documented in the inventory (e.g. `SHELLCHECK_MOCK_FILE`) apply only to the bash wrappers. The native Python analyzers are tested via pytest fixtures — see `tests/python/test_analyzer_<tool>.py` for each tool's test coverage.

