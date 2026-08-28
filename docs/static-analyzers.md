---
layout: default
title: Static Analyzers
parent: Configuration
nav_order: 2
---

# Static Analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path and fall back to sequential when `parallel: false`. If a binary is missing, the native analyzer emits a WARNING to stderr and returns an empty findings array — the review is never blocked.

The container action ships all analyzer binaries pre-installed. For the direct-action or submodule paths, install the binaries you need; see [runtime dependencies](installation-direct-action#runtime-dependencies).

## Controlling which analyzers run

The `analyzers` (allowlist) and `exclude-analyzers` (denylist) settings control which analyzers run. Both accept a comma-separated list of the names in the **Analyzer** column below. Empty (default) means all eligible analyzers run. When `analyzers` is set, `exclude-analyzers` is ignored (allowlist takes precedence). Unknown names are rejected with an error and a suggestion.

These can be set in three places, in precedence order (highest wins):

1. **`/ai-pr-review review-full` slash command** — always runs the full roster, ignoring any allowlist/denylist for that one run.
2. **Action input or repo variable** — set directly in the calling workflow, or via the `AI_ANALYZERS`/`AI_EXCLUDE_ANALYZERS` env vars (see [Configuration → Analyzer and agent selection](configuration#analyzer-and-agent-selection)):

   ```yaml
   - uses: tag1consulting/ai-pr-review@main
     with:
       analyzers: 'semgrep,trufflehog'      # Run only semgrep and trufflehog
       # exclude-analyzers: 'checkov,tflint'  # or: run everything except checkov and tflint
   ```

3. **`.github/ai-pr-review/policy.yml`** — a named policy's `analyzers`/`exclude-analyzers` fields, routed by changed-file path, base branch, or head branch, so different PRs can get different analyzer sets without editing the workflow file. See [Policies](policy) for the full schema.

An explicit action input or repo variable always wins over a `policy.yml` route match; a route match wins over the engine's hard-coded default (all eligible analyzers run). The same three-place precedence applies to the `agents`/`exclude-agents` allowlist/denylist for review agents.

## Category mapping

In addition to severity, every analyzer maps its findings onto the same 11-value category taxonomy used by the LLM agents (`authz`, `injection`, `dependency-cve`, `secret`, `architecture-coupling`, `test-gap`, `edge-case`, `observability`, `docs`, `lint`, `other`). This lets an analyzer finding corroborate an LLM-agent finding on the same issue — corroborated findings are exempt from the LLM judge pass's down-ranking and get a confidence boost. Findings the analyzer can't confidently classify map to `"other"`, which never blocks corroboration with a real category but also never falsely matches one.

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
| **docs-api-check** | Python + `@param`-family languages (JS/TS, Java, Kotlin, C#, Ruby, C++, Scala) | A documented parameter not in the signature, or vice versa, always→Medium | 90 (Python, via ruff) / 80 (tree-sitter engine) | `docs-api-check` |
| **docs-missing-check** | Python, Go, + the same `@param`-family languages | A newly-added public function/method with no doc comment, diff-gated to added lines only, always→Low | 90 (Python) / 80 (Go, tree-sitter engine) | `docs-missing-check` |
| **docs-ref-check** | Changed `.md` files | A broken relative link or heading anchor, always→Medium | 80 | `docs-ref-check` |
| **docs-drift-check** | Any file (runs unconditionally, like semgrep/trufflehog) | A doc reference to a file this PR deletes, always→Low | 80 | `docs-drift-check` |

## Documentation checks

Four analyzers catch documentation mismatch and drift for zero LLM tokens. None can block a PR on their own (`docs-api-check` and `docs-ref-check` are Medium; `docs-missing-check` and `docs-drift-check` are Low, and Medium/Low both resolve to `APPROVE`).

- **`docs-api-check`** compares a function's documented parameters against its actual signature. Python uses the already-installed `ruff` binary with `--isolated` (so results never depend on the consumer's own ruff config); every other supported language uses a shared tree-sitter traversal (JSDoc, JavaDoc, KDoc, YARD, Doxygen, and C# XML doc comment styles are all recognized). A function with a destructured or rest parameter is skipped entirely rather than guessed at.
- **`docs-missing-check`** flags a newly-added public function or method with no doc comment at all, diff-gated to symbols genuinely added in the current diff (not pre-existing undocumented code). Go is checked via a dedicated `golangci-lint --enable-only=godoclint` invocation with `require-doc` explicitly enabled (that rule is opt-in even when the linter itself is on).
- **`docs-ref-check`** and **`docs-drift-check`** are pure-Python, offline, no network calls ever: broken relative links/heading anchors in changed Markdown, and doc references to a file the current PR deletes, respectively.

PHP is deliberately excluded from `docs-api-check`/`docs-missing-check` — `phpcs` already covers doc-comment mismatch on both the Drupal and PSR12 paths (see the `phpcs` row above). See `docs/adr/0001-tree-sitter-not-node-for-doc-mismatch.md` and `docs/adr/0002-hand-rolled-doc-ref-checker-not-lychee.md` in the repo for why these are hand-rolled rather than built on an existing tool.

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

## SARIF ingestion

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

All 13 analyzers are implemented as native Python functions in `ai_pr_review/analyzers/native/`. The `analyzers/bridge.py` dispatcher maps each tool name to its Python callable. Each analyzer invokes the tool binary directly via `subprocess.run` and parses the JSON output in Python. See `tests/python/test_analyzer_<tool>.py` for each tool's test coverage.

