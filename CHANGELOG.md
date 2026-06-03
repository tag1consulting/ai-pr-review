# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-06-03

### Fixed

#### Critical — analyzer bridge passed no input to wrapper scripts (#420)

`ai_pr_review/analyzers/bridge.py` invoked every `run-*.sh` wrapper via
`subprocess.run` without an `input=` argument. All 12 analyzers received an
empty `CHANGED_FILES` and silently returned `[]`. No static-analysis findings
were produced by the Python engine for any PR since v1.0.0 shipped.

The bridge now computes a sorted, deduplicated newline-joined file list via a
new `_file_list()` helper and passes it as `input=` to every subprocess call.
Six new tests cover the helper and the stdin wiring end-to-end.

#### Feedback store wrote empty source/file/rule_id fields (#425)

`slash-commands.yml` was reading the wrong positional token for `source` after
the `**[F{n}]**` finding-ID was inserted between severity and source in inline
comment bodies. The `sed` extractor now strips the F-token before extraction,
matching the body-level lookup path. `build_entry` in `handlers.py` also now
persists `command.finding_id` into `extras["finding_id"]`. Records with no
extractable context are flagged with `extras["context_missing"] = True` and
emit a `logger.warning` in workflow logs.

#### run-shellcheck.sh — file existence guard and jq failure handling (#422)

- Files are now checked for existence before being passed to shellcheck,
  preventing spurious errors on deleted files in the diff.
- A `jq` parse failure on per-file output now emits a WARNING and continues
  to the next file instead of silently dropping remaining findings.

#### run-trufflehog.sh — dual-mode input and robust YAML allowlist parser (#422)

- When `$1` is an existing file on disk, the script runs in diff-file scanning
  mode (passed directly to `trufflehog filesystem`). Otherwise `$1` or stdin
  is treated as a newline-separated changed-files list.
- The YAML allowlist parser is rewritten as an `awk` state machine that
  correctly handles double-quoted, single-quoted, and unquoted path list items
  and exits the `paths:` block cleanly on the next sibling key.

#### run-cve-check.sh — range version truncation and requirements pinning (#423)

- `parse_package_json` and `parse_composer_json`: added a second `gsub` to
  truncate version strings at the first range delimiter so specs like
  `>=1.2.3 <2.0.0` yield a clean `1.2.3` rather than `1.2.3 <2.0.0`.
- `parse_requirements_txt`: restricted to `==` and `===` exact pins only.
  Range specifiers (`>=`, `~=`, `<=`) are skipped -- OSV needs a concrete
  installed version; querying a range boundary produces false positives.
- `sed` operator-strip pattern anchored with `^` to prevent false matches.
- CVSS v4 and v2 vectors now return `null` from `parse_score`, mapping
  conservatively to `High` rather than silently downgrading Critical CVEs.
- New `cvss_display` field renders a numeric score or CVSS version prefix.
- `package.json` dependencies tagged `prod`/`dev`; findings append
  `(dev dependency)` for devDependencies.
- `lib-*` virtual platform packages excluded from Composer OSV queries.

### Added

#### Stdin support for all 12 analyzer wrappers (#420)

All wrappers now accept the changed-files list from a positional argument
(Bash engine) or stdin (Python engine). Wrappers updated: `run-checkov.sh`,
`run-cve-check.sh`, `run-eslint.sh`, `run-golangci-lint.sh`,
`run-hadolint.sh`, `run-kube-linter.sh`, `run-phpcs.sh`, `run-phpstan.sh`,
`run-ruff.sh`, `run-semgrep.sh`, `run-tflint.sh`, `run-trufflehog.sh`.

#### Agent prompt parity with claude-comprehensive-review (#414-#419)

All six agent prompts updated to match the reference implementation:

- **Confidence scoring** (0-100, ≥75 threshold for `json-findings` output)
  added to all agents.
- **Structured `json-findings` schema** with `severity`, `confidence`,
  `file`, `line`, `finding`, `remediation`, `source` fields.
- **Version-checking guardrails**: agents must not flag any
  package/runtime/action/image version as nonexistent or pre-release based
  on training-data recall. Four permitted exceptions documented.
- **`NONE` empty-state**: all agents now output exactly `NONE` when no
  Medium-or-higher findings exist, replacing per-agent prose variations.
- **`NONE:NO_FILES`** sentinel added to `adversarial-general` to distinguish
  missing manifest from clean review.

Per-agent additions beyond the shared set:

| Prompt | Notable additions |
|---|---|
| `pr-summarizer` | Large-diff git fallback, `## Related Issues & PRs` section, cohort grouping headers |
| `edge-case-hunter` | `Read` tool cap (10 calls, 200 lines), over-wide bit-shift case, context-propagation gap |
| `blind-hunter` | Diff-size strategy (<50 lines deep, ≥50 lines breadth-first) |
| `adversarial-general` | Governance block note, explicit specialist-scope delineation, Positive Observations section |
| `architecture-reviewer` | Extended-thinking support, Scope Creep lens (lens 8) |
| `security-reviewer` | GraphQL injection check, `JSON.parse` DoS check, "First Law violations" framing |

#### run-semgrep.sh — stdin support and ruleset strategy documentation (#421)

Stdin support added. Strategy documented: the container ships no baked rule
bundle (Semgrep Rules License v1.0 is use-restricted); falls back to
`--config=auto` at runtime. Operators with permissively-licensed local rules
can point `SEMGREP_RULES_DIR` at their bundle.

### Changed

- `pyproject.toml`: version bumped from `1.0.0` to `1.0.1`.
- `Dockerfile`: tflint updated to v0.58.0 (renovate #424).

### Upgrade Notes

No breaking changes. Users on v1.0.0 will automatically receive working
static-analysis findings after upgrading -- previously all 12 analyzers
were silently returning empty results when invoked through the Python engine.

## [1.0.0] - 2026-06-02

Initial stable release. Python engine is now the default. See the
[v1.0.0 release notes](https://github.com/tag1consulting/ai-pr-review/releases/tag/v1.0.0)
for the full changelog.
