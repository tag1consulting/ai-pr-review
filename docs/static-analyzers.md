---
layout: default
title: Static Analyzers
parent: Configuration
nav_order: 2
render_with_liquid: false
---

# Static Analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path. They run one at a time when `parallel: false`. If a binary is missing, the native analyzer writes a WARNING to stderr and returns an empty findings array. A missing binary never blocks the review.

The container action includes all analyzer binaries except `eslint`. For the direct-action or submodule paths, install the binaries you need. See [runtime dependencies](installation-direct-action#runtime-dependencies).

## Controlling which analyzers run

The `analyzers` (allowlist) and `exclude-analyzers` (denylist) settings control which analyzers run. Both accept a comma-separated list of the names in the **Analyzer** column below. An empty value (default) means all eligible analyzers run. When `analyzers` is set, the action ignores `exclude-analyzers` (the allowlist takes precedence). The action rejects unknown names with an error and a suggestion.

You can set these in three places. The list shows them in precedence order (highest wins):

1. **`/ai-pr-review review-full` slash command**: always runs the full roster and ignores any allowlist or denylist for that one run.
2. **Action input or repo variable**: set it directly in the calling workflow, or with the `AI_ANALYZERS`/`AI_EXCLUDE_ANALYZERS` env vars (see [Configuration → Analyzer and agent selection](configuration#analyzer-and-agent-selection)):

   ```yaml
   - uses: tag1consulting/ai-pr-review@main
     with:
       analyzers: 'semgrep,trufflehog'      # Run only semgrep and trufflehog
       # exclude-analyzers: 'checkov,tflint'  # or: run everything except checkov and tflint
   ```

3. **`.ai-pr-review/policy.yml`** (the action also reads `.github/ai-pr-review/policy.yml` as a fallback): the `analyzers` and `exclude-analyzers` fields of a named policy. The action routes by changed-file path, base branch, or head branch. Different PRs can get different analyzer sets without a change to the workflow file. See [Policies](policy) for the full schema.

An explicit action input or repo variable always wins over a `policy.yml` route match. A route match wins over the engine default (all eligible analyzers run). The same three-place precedence applies to the `agents` and `exclude-agents` allowlist and denylist for review agents.

## Category mapping

Each analyzer maps its findings to a severity and to one of the 12 categories that the LLM agents also use (`authz`, `injection`, `dependency-cve`, `secret`, `crypto`, `architecture-coupling`, `test-gap`, `edge-case`, `observability`, `docs`, `lint`, `other`). An analyzer finding can then corroborate an LLM-agent finding on the same issue. When two different analyzer tools report nearby lines, the review shows one finding and lists the other tool's message under `Also reported by`. A finding shows at most 5 `Also reported by` lines. A last line, `(+N more from other analyzers)`, counts the rest. The LLM judge pass does not down-rank corroborated findings, and they get a confidence boost. If the analyzer cannot classify a finding with confidence, it maps the finding to `"other"`. An `other` finding still corroborates with a finding of a real category, and two `other` findings do not corroborate each other. A `docs` finding, and a finding that the analyzer marks as style only, join only style findings of their own category, so a style finding next to a security finding stays separate. The analyzers decide this per rule, because ruff, eslint, phpcs, shellcheck and hadolint use the category `lint` for style and for defects. The style rules are the phpcs sniffs in the `Commenting`, `WhiteSpace`, `Formatting` and `NamingConventions` categories, the ruff formatting, naming, docstring, import and quote codes, the eslint formatting rules, and the shellcheck and hadolint findings at the `style` level. A rule that finds a defect, such as ruff F821 or eslint `no-eval`, still merges with the agent finding for the same defect. When one semgrep rule fires at two or more places in the same file, the review shows one finding at the first line and lists all the lines in its text (`(4 occurrences in api/user.py, lines: 31, 65, 93, 101)`). Other analyzers roll up when the same message fires more than 5 times in one file.

| Analyzer | Language gate | Severity mapping | Confidence | Source tag |
|----------|--------------|-----------------|------------|------------|
| **shellcheck** | `.sh`, `.bash` | `error`→High, `warning`→Medium | 95 | `shellcheck` |
| **semgrep** | Any file | `ERROR`→High, `WARNING`→Medium, else→Low | 90 | `semgrep` |
| **trufflehog** | Any file | Verified secret→Critical, Unverified→High | 95 / 85 | `trufflehog` |
| **ruff** | `.py` files | `F`/`E` prefix→High, `W`/`C`→Medium, else→Low | 90 | `ruff` |
| **golangci-lint** | `.go` files | `errcheck`/`govet`/`staticcheck`→High, others→Medium | 90 | `golangci-lint` |
| **hadolint** | `Dockerfile*`, `*.dockerfile` | `error`→High, `warning`→Medium, else→Low | 90 | `hadolint` |
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | `CKV2_*` and `CKV_SECRET_*`→High, all other checks→Medium. The analyzer runs checkov from an empty scratch directory with an empty `HOME`, a generated config, and an allowlist environment (search path, locale, temp directory, proxy and CA settings). Job credentials and checkov option variables do not reach it. It never reads a repo `.checkov.yaml`, `.checkov.yml`, baseline file, or external checks, because the analyzed workspace can hold untrusted fork-PR content | 80 | `checkov` |
| **phpcs** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | `ERROR`→High, `WARNING`→Medium, except sniffs in the `Commenting`, `WhiteSpace`, `Formatting` and `NamingConventions` categories, which are Low. The analyzer reads the repo ruleset (`.phpcs.xml`, `phpcs.xml`, `.phpcs.xml.dist`, or `phpcs.xml.dist`) as data only and passes phpcs a generated ruleset. It never lets phpcs read the repo file, because a ruleset can load PHP from the workspace. It keeps the `<file>` scope, exclude patterns, rules that name installed standards or sniffs, and their `exclude`, `severity`, `type` and `properties` settings. With no ruleset, it uses Drupal+DrupalPractice when the repo shows Drupal signals (a `*.info.yml` file, a Drupal 7 `*.info` file with `name` and `core` keys, a `.module`, `.install`, `.theme` or `.profile` file, a composer `type` of `drupal-*`, or a `drupal/core` requirement), else PSR12. If the ruleset names a standard that the image does not have (for example WordPress), phpcs runs a style-neutral correctness fallback and logs the missing standard | 90 | `phpcs` |
| **eslint** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | severity 2→High, severity 1→Medium. Uses the consumer's config, and does nothing if no `eslint.config.*` or `.eslintrc.*` exists | 90 | `eslint` |
| **phpstan** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | Severity depends on `PHPSTAN_LEVEL`: 8–9→High, 5–7→Medium, 0–4→Low (default level 3→Low). The analyzer always runs at that level. It never auto-discovers a project's own `phpstan.neon` or `phpstan.neon.dist`, and it does not run the repo's `vendor/autoload.php`. It starts in an empty scratch directory with an allowlist environment, because the analyzed workspace can hold untrusted fork-PR content. Project classes are not resolved, so "not found" messages are dropped | 85 | `phpstan` |
| **kube-linter** | `.yaml`, `.yml`, `.json` with `apiVersion:` + `kind:` headers | All findings→Medium (reliability-focused: missing probes, resource limits, etc.) | 85 | `kube-linter` |
| **tflint** | `.tf`, `.tfvars` | `error`→High, `warning`→Medium, `notice`→Low. Runs per Terraform module directory | 90 | `tflint` |
| **dep-exists** | Newly added dependencies in `package.json`, `requirements*.txt`, `Cargo.toml`, `composer.json`, `Gemfile` | A dependency the public registry does not know→High, a package first published under 30 days ago (npm, PyPI, crates.io)→Medium | 85 / 65 | `dep-exists` |
| **docs-api-check** | Python + `@param`-family languages (JS/TS, Java, Kotlin, C#, Ruby, C++, Scala) | A documented parameter not in the signature, or vice versa, always→Medium | 90 (Python, via ruff) / 80 (tree-sitter engine) | `docs-api-check` |
| **docs-ref-check** | Changed `.md` files | A broken relative link or heading anchor, always→Medium | 80 | `docs-ref-check` |
| **docs-drift-check** | Any file (runs unconditionally, like semgrep/trufflehog) | A doc reference to a file this PR deletes, always→Low | 80 | `docs-drift-check` |
| **suppression-check** | Source files (not tests, docs or config). Reads added lines only | A comment on an added line turns off a security rule: `noqa` with an `S` code, `nosec`, `nosemgrep`, `phpcs:ignore` or `phpcs:disable` on a security sniff, `eslint-disable` on a security rule, always→Medium | 85 | `suppression-check` |
| **cve-check** | Dependency manifests and lockfiles (see [Dependency vulnerability check](#dependency-vulnerability-check)) | CVSS >= 9.0→Critical, 7.0–8.9→High, 4.0–6.9→Medium, below 4.0→Low, unscored→High | 90 (70 when unscored) | `osv` |

## Dependency existence check

`dep-exists` asks the public registry whether each dependency this diff adds really exists. A model can invent a package name, and an attacker can register that name and ship malicious code under it. The check makes network calls to `registry.npmjs.org`, `pypi.org`, `crates.io`, `repo.packagist.org`, and `rubygems.org`. It sends only the package name.

- It checks only dependencies on added lines of a direct-dependency manifest. The analyzer rebuilds the manifest as it was before the change, and a name that the old manifest already declared as a registry dependency (a version bump, or a move between sections) is not new, so it is not checked. A name that was a local path, a workspace member, or a git source and now names a registry version is checked. If the same change touches `.npmrc`, `.yarnrc`, or `.cargo/config`, or moves the manifest, or the old requirements file or Gemfile used a private index or source, every added dependency is checked. If the diff does not match the file, every added dependency is checked. It does not read lockfiles.
- It does not check Go modules, because a private Go module returns "not found" from the public proxy and would raise a false finding.
- It skips an ecosystem when the repository sets a private registry (`.npmrc` registry, pip index flags, Cargo registries, composer `repositories`, a non-default Gemfile `source`).
- It fails open. A timeout, a rate limit, or any status other than 200 or 404 gives no finding.
- It reads only files in the repository, so it cannot see a private registry that is set in CI settings or a user-level config. A private scoped npm package, or a monorepo package that another package lists by plain version, returns "not found" from the public registry. The result is a High finding, and a High finding makes the review request changes. Set the registry in a file in the repository, or turn the analyzer off.
- It checks `peerDependencies` too, because npm 7 and later installs them. A peer is therefore looked up on the public registry like any other dependency, and a private peer package gets the same High finding. A name that moves from `peerDependencies` to `dependencies` is looked up, because a peer is not always installed.
- It checks at most 25 new dependencies per run.
- It reads the paths in the diff with a shared parser (`ai_pr_review/diff/parse.py`). The engine pins the git diff format (`a/` and `b/` prefixes, unquoted non-ASCII paths), so a user's git config cannot hide a manifest. A path that holds a control character is never decoded. The analyzer cannot read such a path and logs a warning with a count of the files it skipped.
- Turn it off with `exclude-analyzers: dep-exists`.

### What the check does not prove

`dep-exists` is an advisory check, not a control that stops a dependency-confusion attack. It works from the diff and the files on disk. It does not know where a dependency resolves from in your build.

- **What it can catch.** A name that no public registry knows, and a package first published in the last 30 days. Both are signs of a name that a model invented or that an attacker registered.
- **What it cannot catch.** A name that exists on the public registry and is not the package you mean (a lookalike that is older than 30 days). A name that you also publish on a private registry. A change in a transitive dependency or a lockfile. A Go module.
- **How it tells a new dependency from an old one.** It rebuilds the old manifest by undoing the diff hunks, which always matches the diff it was given. It does not read the base commit. It checks every added dependency when it cannot trust the old names: the hunks do not fit the file, the manifest moves, the same change edits a registry config file (`.npmrc`, `.yarnrc`, `.cargo/config`), or the old file used a private index or source.
- **How it fails.** It fails open on a lookup that gets no usable answer, and it logs one warning with the count. A High finding makes the review request changes, so a private package that the public registry does not know can block a pull request. Set the registry in a file in the repository, or turn the analyzer off.
- **Follow-up work** is tracked in [#1033](https://github.com/tag1consulting/ai-pr-review/issues/1033): read the base manifest from git, an opt-in list of private scopes, and a decision on whether the check may request changes.

## Documentation checks

Three analyzers find documentation mismatch and drift for zero LLM tokens. None can block a PR alone. `docs-api-check` and `docs-ref-check` report Medium, `docs-drift-check` reports Low, and Medium and Low both resolve to `APPROVE`.

- **`docs-api-check`** compares the documented parameters of a function with its actual signature. Python uses the installed `ruff` binary with `--isolated`, so the results never depend on the consumer's ruff config. Every other supported language uses a shared tree-sitter traversal. The traversal recognizes the JSDoc, JavaDoc, KDoc, YARD, Doxygen, and C# XML doc comment styles. The analyzer skips a function with a destructured or rest parameter and does not guess.
- **`docs-ref-check`** finds broken relative links and heading anchors in changed Markdown.
- **`docs-drift-check`** finds doc references to a file that the current PR deletes.

`docs-ref-check` and `docs-drift-check` are pure Python and offline. They never make network calls.

`docs-api-check` excludes PHP on purpose, because `phpcs` already covers doc-comment mismatch on the Drupal and PSR12 paths (see the `phpcs` row above). See `docs/adr/0001-tree-sitter-not-node-for-doc-mismatch.md` and `docs/adr/0002-hand-rolled-doc-ref-checker-not-lychee.md` in the repo for the reasons these analyzers are hand-written and do not use an existing tool.

A fourth analyzer, **`docs-missing-check`**, flagged a newly added public function or method that had no doc comment. It checked only symbols that the current diff added. It checked Go with a dedicated `golangci-lint --enable-only=godoclint` invocation. The maintainers removed it in issue #815. It reported Low severity only, and its maintenance cost (a separate ruff rule set, the dedicated Go linter invocation, and a second tree-sitter presence check) was too high for its review value. It was not redundant with the other three analyzers. `docs-api-check` fires only when a doc comment already exists and does not match the signature. See the #815 PR description for the full reasoning. The `analyzers` and `exclude-analyzers` settings still accept the name as a deprecated no-op. The v3.0.0 release removes it.

## Dependency vulnerability check

The analyzer name for `analyzers` and `exclude-analyzers` is `cve-check`. When a PR modifies a supported dependency manifest or lockfile, the action queries [OSV.dev](https://osv.dev/) for known vulnerabilities in the declared versions. It reports them as findings next to the LLM review. The action prefers lockfiles to range manifests when both exist.

| Ecosystem | Supported files |
|-----------|-----------------|
| Go | `go.mod` |
| npm | `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml` |
| Python | `poetry.lock`, `Pipfile.lock`, `uv.lock`, `requirements*.txt` (exact pins only) |
| PHP | `composer.lock`, `composer.json` |
| Rust | `Cargo.lock` |
| Ruby | `Gemfile.lock` |

The analyzer maps CVSS scores to severity: >= 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, below 4.0 → Low. Unscored or unparseable CVEs map to **High** as a fail-safe (see `_cvss_to_severity` in `ai_pr_review/analyzers/native/cve_check.py`). The source tag is `osv`. Confidence is 90, or 70 when the CVE has no score. Critical and High findings trigger `REQUEST_CHANGES` on the PR review, like any other high-severity finding.

The check needs no configuration. It runs when a manifest file is in the diff. The OSV.dev API is unauthenticated and free. If the API is unreachable, the check logs a warning and continues. A CVE-lookup failure never blocks the review.

To accept a specific CVE (for example, a library that only a test fixture uses), add a suppression rule that matches the CVE or GHSA ID. See [Suppression rules](suppression#suppressing-cve-findings) for the schema and a worked example.

## SARIF ingestion

The Python engine can also read SARIF 2.1.0 output from any external tool (CodeQL, Semgrep Pro, Trivy, Snyk, custom scanners).

### Setup

1. Run your SARIF-producing tool as an earlier step (for example, CodeQL or Trivy).
2. Pass the output paths in the `sarif-paths` input:

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

- Source tag: `sarif:<driver.name>` (for example, `sarif:CodeQL`, `sarif:trivy`).
- Default confidence: 90.
- Remediation text: taken from the rule's `help.text` field when present.
- File URI prefixes (`file:///`, `file://`) are stripped from location paths.
- Findings from SARIF files go through the same dedup and suppress pipeline as findings from native analyzers and LLM agents.
- The engine logs a `WARNING` for an unreadable or malformed SARIF file and skips it (fail-soft).

---

## Implementation reference

All 18 analyzers are native Python functions in `ai_pr_review/analyzers/native/`. The `analyzers/bridge.py` dispatcher maps each tool name to its Python callable. Each analyzer runs the tool binary with `subprocess.run` and parses the JSON output in Python. Test coverage is in `tests/python/test_analyzer_<tool>.py`. The three docs analyzers use the files `test_analyzer_docs_comments.py` (docs-api-check), `test_analyzer_docs_ref_check.py`, and `test_analyzer_docs_drift_check.py`.

