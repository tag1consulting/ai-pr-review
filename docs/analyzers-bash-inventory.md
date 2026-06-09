---
layout: default
title: Analyzer Bash Wrapper Inventory
nav_order: 6
---

# Analyzer Bash Wrapper Inventory

Each of the 13 static analyzers ships as a thin bash wrapper in `analyzers/run-<tool>.sh`. At runtime, `ai_pr_review/analyzers/bridge.py` dispatches to these wrappers via `subprocess.run(["bash", script_path], ...)` and `json.loads` the stdout. The Python engine itself does no native parsing of analyzer output — that transformation lives entirely inside each wrapper.

This document records the implementation details of every wrapper: binary invocation, input handling, output-field mapping, known complexity, and the mock mechanism used in tests. It exists primarily to scope a future native-Python port (tracked as Phase 11 issues) and to serve as the authoritative reference for parity requirements.

**Architecture note:** `bridge.py` invokes each wrapper with the changed-file list passed via **stdin as a newline-separated string of relative paths** (`_file_list()` returns `"\n".join(sorted(set(cf.all_files)))`), and `DIFF_FILE` set in the environment pointing to the raw diff. Wrappers that do not operate on individual files (e.g. trufflehog, cve-check) ignore stdin and read from the workspace root or the diff. All wrappers write JSON to stdout conforming to the `json-findings` schema described below.

---

## Output schema

Every wrapper emits a single JSON object on stdout with this shape:

```json
{
  "findings": [
    {
      "severity":    "Critical|High|Medium|Low",
      "confidence":  <integer 0-100>,
      "source":      "<tool-name>",
      "file":        "<relative path or empty>",
      "line":        <integer or 0>,
      "finding":     "<short title>",
      "remediation": "<explanation or empty>"
    }
  ]
}
```

`cve-check` is the sole exception: its findings carry an additional `"agent": "dependency-check"` field and use `"source": "osv"` (not `"cve-check"`).

Wrappers that find nothing emit `{"findings": []}`. Wrappers that cannot run (binary absent) emit a WARNING to stderr and return the same empty object — the review is never blocked.

---

## Wrapper reference

### shellcheck

| Field | Value |
|---|---|
| Binary | `shellcheck` |
| Invocation | `shellcheck -f json1 -S warning <files...>` |
| Input | `$1` — space-separated list of `.sh`/`.bash` files in the diff |
| Confidence | 95 |
| Source tag | `shellcheck` |
| Mock env var | `SHELLCHECK_MOCK_FILE` |
| Fixture dir | `tests/fixtures/shellcheck/` |

**Severity mapping:** `error` → High, `warning` → Medium, `style`/`info` → Low.

**Path handling:** `shellcheck` outputs paths exactly as passed; no normalization needed.

**Complexity: LOW.** The wrapper iterates changed `.sh`/`.bash` files, runs shellcheck with `-f json1`, and reshapes the `code`/`level`/`message` fields. The severity map is a constant lookup. No config discovery, no network.

---

### ruff

| Field | Value |
|---|---|
| Binary | `ruff` |
| Invocation | `ruff check --output-format json <files...>` |
| Input | `$1` — space-separated list of `.py` files in the diff |
| Confidence | 90 |
| Source tag | `ruff` |
| Mock env var | `RUFF_MOCK_FILE` |
| Fixture dir | `tests/fixtures/ruff/` |

**Severity mapping:** rule code prefix determines severity — `F` (Pyflakes) or `E` (pycodestyle error) → High; `W` (warning) or `C` (convention) → Medium; anything else → Low.

**Path handling:** ruff outputs absolute paths; the wrapper strips the `$GITHUB_WORKSPACE/` prefix.

**Complexity: LOW.** Single-pass JSON reshape with a prefix-based severity map. No discovery, no network, no multi-step logic.

---

### hadolint

| Field | Value |
|---|---|
| Binary | `hadolint` |
| Invocation | `hadolint --format json <files...>` |
| Input | `$1` — space-separated list of `Dockerfile*` / `*.dockerfile` files in the diff |
| Confidence | 90 |
| Source tag | `hadolint` |
| Mock env var | `HADOLINT_MOCK_FILE` |
| Fixture dir | `tests/fixtures/hadolint/` |

**Severity mapping:** `error` → High, `warning` → Medium, `info`/`style` → Low.

**Path handling:** hadolint outputs paths as given; no normalization.

**Complexity: LOW.** Pure reshape of hadolint's JSON output. No config discovery, no network.

---

### kube-linter

| Field | Value |
|---|---|
| Binary | `kube-linter` |
| Invocation | `kube-linter lint --format json <files...>` |
| Input | `$1` — files from the diff; eligibility pre-filtered by grep for `apiVersion:` + `kind:` |
| Confidence | 85 |
| Source tag | `kube-linter` |
| Mock env var | `KUBELINTER_MOCK_FILE` |
| Fixture dir | `tests/fixtures/kubelinter/` |

**Severity mapping:** All findings → Medium (kube-linter does not emit severity levels; reliability findings are uniformly medium).

**Eligibility sniff:** Before invoking kube-linter, the wrapper greps the changed `.yaml`/`.yml`/`.json` files for both `apiVersion:` and `kind:` to skip non-Kubernetes manifests.

**Path handling:** kube-linter outputs the path as passed; no normalization.

**Complexity: LOW-MED.** The k8s content sniff adds a grep pre-pass, but the JSON reshape is straightforward. No network.

---

### phpcs

| Field | Value |
|---|---|
| Binary | `phpcs` |
| Invocation | `phpcs --report=json --standard=<Drupal+DrupalPractice\|PSR12> <files...>` |
| Input | `$1` — space-separated list of `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` files |
| Confidence | 90 |
| Source tag | `phpcs` |
| Mock env var | `PHPCS_MOCK_FILE` |
| Fixture dir | `tests/fixtures/phpcs/` |

**Severity mapping:** phpcs `ERROR` → High, `WARNING` → Medium.

**Standard discovery:** The wrapper probes `phpcs -i` output; if `Drupal` and `DrupalPractice` appear, it uses `--standard=Drupal,DrupalPractice`. Otherwise it falls back to `PSR12`. The selected standard is reported in the finding `source` suffix (e.g. `phpcs:Drupal`... actually no — `source` is always the flat string `phpcs`; the standard selection affects which rules fire, not the source tag).

**Path handling:** phpcs emits absolute paths; the wrapper strips `$GITHUB_WORKSPACE/` (or `$PWD/` if workspace is unset) to produce relative paths.

**Complexity: LOW-MED.** Standard discovery via `phpcs -i` is a subprocess call, and absolute-path stripping is a string operation. No network.

---

### semgrep

| Field | Value |
|---|---|
| Binary | `semgrep` |
| Invocation | `semgrep --json --config=<rule-bundle\|auto> <files...>` |
| Input | `$1` — space-separated list of changed files |
| Confidence | 90 |
| Source tag | `semgrep` |
| Mock env var | `SEMGREP_MOCK_FILE` |
| Fixture dir | `tests/fixtures/semgrep/` |

**Severity mapping:** `ERROR` → High, `WARNING` → Medium, any other level → Low.

**Rule bundle discovery:** The wrapper checks for a `SEMGREP_RULES` env var first. If absent, it checks for a `.semgrep/` or `semgrep.yml` in the workspace. If neither exists, it falls back to `--config=auto`, which contacts the semgrep registry over the network.

**Network dependency:** The `auto` fallback downloads rules at scan time. In air-gapped or rate-limited environments this may time out; the wrapper tolerates a non-zero exit and returns an empty findings array.

**Path handling:** semgrep outputs paths relative to the working directory; no normalization needed.

**Complexity: MED.** Rule-bundle discovery logic (3 paths) and the network fallback add meaningful complexity over a pure reshape. A Python port must preserve the offline/auto fallback behavior.

---

### golangci-lint

| Field | Value |
|---|---|
| Binary | `golangci-lint` |
| Invocation | `golangci-lint run --out-format json ./...` (or targeted by package) |
| Input | `$1` — changed `.go` files; wrapper derives the module root and package set |
| Confidence | 90 |
| Source tag | `golangci-lint` |
| Mock env var | `GOLANGCI_MOCK_FILE` |
| Fixture dir | `tests/fixtures/golangci/` |

**Severity mapping:** linter name determines severity — `errcheck`, `govet`, `staticcheck` → High; all others → Medium.

**Module root discovery:** The wrapper walks up from each changed file to find the nearest `go.mod`, then invokes golangci-lint from that directory. Multiple modules in a single PR are handled by deduplicating roots and running once per root.

**Package pattern derivation:** Changed files are converted to `./internal/pkg/...`-style patterns to limit the scan to affected packages.

**Path handling:** golangci-lint output paths are relative to the module root; the wrapper prepends the module root's relative path to produce workspace-relative paths.

**Complexity: MED.** Module-root walking + package-pattern derivation + path-prefix prepending are non-trivial transformations. No network.

---

### checkov

| Field | Value |
|---|---|
| Binary | `checkov` |
| Invocation | `checkov -d <workspace> --output json --compact` (optionally `--check <selected-checks>`) |
| Input | Operates on the workspace directory, filtered to changed IaC files |
| Confidence | 80 |
| Source tag | `checkov` |
| Mock env var | `CHECKOV_MOCK_FILE` |
| Fixture dir | `tests/fixtures/checkov/` |

**Severity mapping:** Check ID prefix → `CKV2_*` or `CKV_SECRET_*` → High; all other checks → Medium.

**IaC eligibility sniff:** The wrapper greps changed files for Terraform (`.tf`/`.tfvars`), Kubernetes/Helm (YAML with `apiVersion:`/`kind:`), Dockerfiles, and CloudFormation/CDK JSON before invoking checkov.

**Output normalization:** checkov can emit either a JSON object or a JSON array depending on framework count. The wrapper normalizes both to a flat findings list.

**Path handling:** checkov emits paths relative to the `-d` directory argument; the wrapper strips the leading `/` that checkov prepends in some output variants.

**Complexity: MED.** Content-sniff regexes + object/array output normalization + path-stripping add up. Confidence is intentionally lower (80) than other tools because checkov has a higher false-positive rate.

---

### phpstan

| Field | Value |
|---|---|
| Binary | `phpstan` |
| Invocation | `phpstan analyse --error-format=json --level=<N> <files...>` |
| Input | `$1` — changed PHP-family files |
| Confidence | 85 |
| Source tag | `phpstan` |
| Mock env var | `PHPSTAN_MOCK_FILE` |
| Fixture dir | `tests/fixtures/phpstan/` |

**Severity mapping:** All findings → High (phpstan has no native severity levels).

**Level:** The wrapper honors `PHPSTAN_LEVEL` env var (default 3). If a `phpstan.neon` or `phpstan.neon.dist` is present at workspace root, phpstan uses the file's `level` setting and the env var is ignored.

**Autoload discovery:** If `vendor/autoload.php` is present, the wrapper adds `--autoload-file=vendor/autoload.php`. If the file is absent, phpstan still runs (useful for projects that pre-install via composer before CI).

**Config discovery:** If `phpstan.neon` or `phpstan.neon.dist` exists, the wrapper passes `--configuration=<path>` explicitly to ensure it is picked up when the working directory differs from the project root.

**Path handling:** phpstan emits absolute paths; the wrapper strips `$GITHUB_WORKSPACE/` or `$PWD/`.

**Complexity: MED.** Config discovery + conditional autoload + level validation make this moderately complex. No network.

---

### eslint

| Field | Value |
|---|---|
| Binary | `eslint` (local `node_modules/.bin/eslint` preferred, then `npx eslint`, then global) |
| Invocation | `eslint --format json <files...>` |
| Input | `$1` — changed `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` files |
| Confidence | 90 |
| Source tag | `eslint` |
| Mock env var | `ESLINT_MOCK_FILE` |
| Fixture dir | `tests/fixtures/eslint/` |

**Severity mapping:** eslint severity integer → `2` (error) → High; `1` (warning) → Medium.

**Binary resolution:** The wrapper checks (in order): `node_modules/.bin/eslint`, `npx eslint`, `eslint` on `PATH`. If none exists, it exits silently with an empty findings array. This allows the action to be used in repos without eslint without failing.

**Config discovery:** The wrapper checks for `eslint.config.js`, `eslint.config.mjs`, `.eslintrc.js`, `.eslintrc.json`, `.eslintrc.yml`, or `.eslintrc.yaml`. If no config is found, eslint is not invoked (the wrapper returns empty) — this is intentional: without a config the consumer did not opt into eslint.

**Version probe:** eslint v8 uses `--format json`; eslint v9 changed the flat config format. The wrapper probes `eslint --version` and selects the appropriate `--format` flag.

**Path handling:** eslint outputs absolute paths; the wrapper strips the workspace prefix.

**Complexity: MED.** Binary resolution (3 paths) + config-presence gate + version capability probe make this one of the more complex wrappers despite its straightforward JSON output.

---

### tflint

| Field | Value |
|---|---|
| Binary | `tflint` |
| Invocation | `tflint --format json` (run once per Terraform module directory) |
| Input | `$1` — changed `.tf`/`.tfvars` files; wrapper derives unique parent directories |
| Confidence | 90 |
| Source tag | `tflint` |
| Mock env var | `TFLINT_MOCK_FILE` |
| Fixture dir | `tests/fixtures/tflint/` |

**Severity mapping:** `error` → High, `warning` → Medium, `notice` → Low.

**Per-directory invocation:** tflint operates on a single Terraform module directory at a time. The wrapper extracts unique parent directories from the changed-file list and invokes tflint once per directory, then merges results.

**Filename reconstruction:** tflint's JSON output includes a `filename` field relative to the directory it was invoked from. The wrapper prepends the directory path to produce workspace-relative paths.

**Multi-dir deduplication:** When the same finding appears in multiple directories (unusual but possible with symlinks), the wrapper deduplicates by `file:line:finding`.

**Complexity: MED.** Per-directory invocation + filename reconstruction + deduplication require careful sequencing. No network for the runner itself (tflint plugins may fetch rules, but that happens in `tflint --init`, not `tflint --format json`).

---

### trufflehog

| Field | Value |
|---|---|
| Binary | `trufflehog` |
| Invocation | `trufflehog filesystem --json <workspace>` (or `trufflehog git file://<workspace> --json --since-commit <base> --branch <head>`) |
| Input | Workspace root; optionally scoped to git history between base and head SHAs |
| Confidence | 95 (verified), 85 (unverified) |
| Source tag | `trufflehog` |
| Mock env var | `TRUFFLEHOG_MOCK_FILE` |
| Fixture dir | `tests/fixtures/trufflehog/` |

**Severity mapping — multi-axis:**
- Verified secret → Critical
- Unverified, not a test credential → High
- Unverified, classified as a test credential → Medium (demoted)
- "Test" classification uses trufflehog's `IsTest` field and a secondary heuristic (detector name or detector ID contains `test`/`sample`/`fake`)

**Scan mode:** When `BASE_SHA` and `HEAD_SHA` are set, the wrapper uses `trufflehog git` mode to scan only the commits in the diff. Otherwise it falls back to `trufflehog filesystem` scanning the full workspace. The git mode is faster and reduces noise from pre-existing secrets.

**Allowlist (YAML parser in bash):** The wrapper reads a `trufflehog-allowlist.yml` file from workspace root (if present) and suppresses matching findings. The YAML parser is implemented in awk: it reads `detector:`/`raw:` key-value pairs into an associative array and compares against each finding. This is the most complex piece of the bash layer: the awk parser handles multi-line YAML lists and quoted strings.

**Path handling:** trufflehog outputs a `SourceMetadata.Data.Filesystem.file` or `Git.file` path. The wrapper normalizes both variants and strips the workspace prefix.

**Confidence is finding-level:** The wrapper assigns confidence 95 to verified findings and 85 to unverified ones (unlike most wrappers which use a single global constant).

**Complexity: HIGH.** Dual scan-mode selection, multi-axis severity demotion (verified/unverified/test), and the awk YAML allowlist parser are all non-trivial. A Python port should replace the allowlist parser with PyYAML, replace the multi-axis severity logic with an explicit decision table, and handle the `trufflehog git` vs `filesystem` mode selection. The per-finding confidence assignment must be preserved.

---

### cve-check

| Field | Value |
|---|---|
| Binary | None — queries the [OSV.dev](https://osv.dev/) batch API directly via `curl` |
| Invocation | `curl -s -X POST https://api.osv.dev/v1/querybatch` |
| Input | Changed manifest files in the diff: `go.mod`, `package.json`, `requirements.txt`, `composer.json` |
| Confidence | Variable (see below) |
| Source tag | **`osv`** (not `cve-check`) |
| Additional field | `"agent": "dependency-check"` on every finding |
| Mock env var | `OSV_MOCK_FILE` |
| Fixture dir | `tests/fixtures/cve/` |

**Source tag exception:** This is the only wrapper where `source` is not the wrapper's own name. It always emits `"source": "osv"` and adds `"agent": "dependency-check"`. Both fields are required for parity.

**Lockfile parsers (4 ecosystems):**
- `go.mod` — regex-extracts `require` blocks and indirect dependencies; ecosystem `Go`
- `package.json` — `jq` parses `.dependencies` and `.devDependencies`; ecosystem `npm`
- `requirements.txt` — line-by-line with `==` version pinning; ecosystem `PyPI`
- `composer.json` — `jq` parses `.require` and `.require-dev`; ecosystem `Packagist`

**OSV batch API:** Packages are submitted in a single `querybatch` POST. The API returns a parallel array of vulnerability lists; the wrapper re-aligns results by index (OSV may return fewer objects than submitted if some have no hits — the wrapper handles this offset by checking `vulns` array presence).

**CVSS v3.1 scorer (in jq):** The wrapper extracts CVSS v3.1 base scores from OSV's `severity` array (type `CVSS_V3`) and maps to finding severity: >= 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, < 4.0 → Low. If no CVSS v3 score is available it falls back to CVSS v2, then to a heuristic based on the `database_specific.severity` string. Unknown severity defaults to **High** (fail-safe).

**Confidence:** 90 for findings with a CVSS score; 70 for findings scored only via heuristic.

**Network dependency:** Every run makes a live HTTPS request to `api.osv.dev`. The mock env var (`OSV_MOCK_FILE`) replaces the curl call in tests. A Python port should use `httpx` with retry logic and must preserve the batch-API alignment logic.

**Complexity: HIGH.** Four separate lockfile parsers, batch HTTP with index-realignment, a CVSS v3.1 scorer, a fallback severity heuristic, and the `source:"osv"`/`agent:"dependency-check"` exception combine to make this the most complex wrapper in the set. It is also the most valuable port candidate: replacing the bash/curl/jq/awk stack with Python + httpx + packaging ecosystem parsers would be substantially more robust and testable. The port likely warrants sub-stories per ecosystem (go.mod, npm, PyPI, composer).

---

## Parity constants summary

When porting any wrapper to native Python, these constants must be reproduced exactly. They are checked by the existing bats fixtures and will be checked by future Python pytest fixtures.

| Wrapper | `source` tag | Confidence | Severity default (unknown) |
|---------|-------------|-----------|---------------------------|
| shellcheck | `shellcheck` | 95 | Low |
| ruff | `ruff` | 90 | Low |
| hadolint | `hadolint` | 90 | Low |
| kube-linter | `kube-linter` | 85 | Medium |
| phpcs | `phpcs` | 90 | Medium |
| semgrep | `semgrep` | 90 | Low |
| golangci-lint | `golangci-lint` | 90 | Medium |
| checkov | `checkov` | 80 | Medium |
| phpstan | `phpstan` | 85 | High (all findings are High) |
| eslint | `eslint` | 90 | Medium |
| tflint | `tflint` | 90 | Low |
| trufflehog | `trufflehog` | 95/85 (per finding) | N/A (always explicit) |
| cve-check | **`osv`** | 90 or 70 (per finding) | **High** (fail-safe) |

---

## Recommended port order

Phase 11 issues are filed per analyzer. The recommended sequencing is LOW → MED → HIGH to let the LOW-complexity ports establish the native-analyzer pattern in `bridge.py` before tackling the more complex wrappers.

**Batch 1 (LOW):** shellcheck, ruff, hadolint — establish the Python native-analyzer shape; each replaces a subprocess call in `bridge.py` with a function that runs the binary, parses its JSON, and returns a `list[Finding]`.

**Batch 2 (LOW-MED):** kube-linter, phpcs — add eligibility pre-sniff and config-discovery patterns.

**Batch 3 (MED):** semgrep, golangci-lint, checkov, phpstan, eslint, tflint — each has a distinct non-trivial behavior to preserve; port independently.

**Batch 4 (HIGH):** trufflehog (allowlist parser + severity matrix), cve-check (lockfile parsers + OSV HTTP + CVSS scorer) — these are the highest-value and highest-risk ports; each should have its own sub-milestone with explicit acceptance tests before merging.

See [Phase 11 tracking issue](https://github.com/tag1consulting/ai-pr-review/issues) and the per-analyzer stories for full acceptance criteria.
