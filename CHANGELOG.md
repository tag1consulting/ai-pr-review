# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.0] - 2026-06-09

### Changed

#### All 13 static analyzers ported to native Python (Epic 8, closes #462–#474)

Every static analyzer that previously ran as a bash subprocess via `analyzers/run-<tool>.sh` is now implemented as a native Python function in `ai_pr_review/analyzers/native/`. The `analyzers/bridge.py` dispatcher maps each tool name to its Python callable; the bash wrappers still exist for the deprecated bash engine but are no longer invoked by the default Python engine.

The ported analyzers, in order: shellcheck (#462), ruff (#463), hadolint (#464), kube-linter (#465), phpcs (#466), semgrep (#467), golangci-lint (#468), checkov (#469), phpstan (#470), eslint (#471), tflint (#472), trufflehog (#473), cve-check (#474).

**What changed at runtime:** The Python engine no longer shells out to bash for any static analysis. Each tool binary is still invoked via `subprocess.run`, but the output is now parsed directly in Python rather than through awk/jq/sed pipelines. Eligibility gating (changed-files filtering, content-sniff checks) has moved from bash into the Python functions.

**What is unchanged:** Findings schema, severity mappings, confidence values, and source tags are all parity-identical to the bash wrappers. The `analyzers/run-<tool>.sh` wrappers are unchanged and continue to work for the deprecated bash engine.

**Testing:** Each native analyzer has a corresponding `tests/python/test_analyzer_<tool>.py` pytest module with equivalent coverage to the bats fixtures.

## [1.3.0] - 2026-06-08

### Changed

#### Slash-command replies now post as `github-actions[bot]`

Slash-command and learning-loop replies (reactions, help text, lookup results, false-positive confirmations) previously posted as the user who owns the `GH_TOKEN` PAT. They now post as `github-actions[bot]` by using a split-token approach: the built-in `GITHUB_TOKEN` handles all plain comment posts, reactions, reads, and label changes, while the PAT is retained only for the `dismiss` command's `resolveReviewThread` GraphQL mutation and review-dismissal REST calls, which `GITHUB_TOKEN` cannot perform on `pull_request_review_comment` events.

Callers of the `slash-commands.yml` reusable workflow must now pass an additional `actions-token` secret. Update your wrapper:

```yaml
secrets:
  github-token: ${{ secrets.GH_TOKEN }}       # PAT, required for dismiss only
  actions-token: ${{ secrets.GITHUB_TOKEN }}  # built-in token for replies
```

The `examples/workflows/comment-triggers.yml` starter template is updated accordingly. No new secret creation is required — `secrets.GITHUB_TOKEN` is available in every repository.

A small known edge: a few confirmation messages inside the dismiss-path steps (the resolve/dismiss outcome replies interleaved in the same shell step as the GraphQL mutation) still post under the PAT. All other replies, including the `false-positive`/`wont-fix`/`lookup` replies that prompted this fix, now post as `github-actions[bot]`.

#### `ignore-merge-commits` now defaults to `true` (closes #448)

Merge commits that pull upstream base-branch changes into a PR are noise: they re-introduce diffs that were already present on the base branch and already reviewed there. Defaulting `ignore-merge-commits` to `true` means only the PR author's own commits are reviewed by default, which is what most teams want.

**Breaking change for existing consumers**: if your PRs contain base-branch merge commits and you rely on them appearing in the diff, set `ignore-merge-commits: false` (or the repo variable `AI_REVIEW_IGNORE_MERGE_COMMITS=false`) to restore the previous behavior.

Affects all three engines and all three VCS providers. Intra-PR merges (merging one feature branch into another) are still preserved regardless of this setting.

#### Context enrichment now defaults to `true` in the container image (closes #391)

The container image ships `tree-sitter-language-pack` and `ripgrep`, so the dependencies required for context enrichment are always present. The `context-enrichment` input (and `AI_CONTEXT_ENRICHMENT` env var) now defaults to `true` in `container-action/action.yml`.

Direct-action consumers (those using `uses: tag1consulting/ai-pr-review@...` without the container image) keep the `false` default because tree-sitter and ripgrep are not guaranteed in that environment. If either dependency is missing, enrichment silently no-ops — no error is thrown and the review proceeds normally.

To opt out in the container image: `context-enrichment: 'false'`.

Python engine only.

#### issue-linker now pre-fetches the open-issue list via `gh issue list` (closes #446)

The issue-linker agent previously emitted raw `<tool_call>` XML when its prompt instructed the model to run `gh issue list` — a command the text-completion call path cannot execute.

The fix is deterministic: Python now runs `gh issue list --state open --limit 50` via subprocess **before** the LLM call and injects the result as a plain-text `## Open Issues` block in the user message. The model never calls any tools; it assesses the pre-fetched list the same way it already assessed the commit log. The `gh` CLI is already installed in the container image and `GH_TOKEN` is already set at runtime, so no new permissions or secrets are required (the `issues: write` permission in the example workflows implies the `read` access needed here).

Behaviour changes:
- The `### Linked Issues` table now fills in real issue titles when the referenced `#N` appears in the open list (rather than "`title not available`").
- The `### Potentially Related` section now surfaces real open issues by number and title when their title or labels match extracted keywords — no fabricated `#` references.
- The fetch is fail-soft: if `gh` is absent, times out, or returns an error, the section falls back to `(unavailable)` and analysis continues from the commit log and manifest alone.

Python engine only.

#### `AI_TEMPERATURE` is now honored in Python engine LLM requests (closes #356)

The `AI_TEMPERATURE` environment variable (and `temperature` action input) was already read and validated by the Python engine but was never passed to the `LLMRequest` that each agent, the pr-summarizer, and the issue-linker sends to the LLM provider. All three now receive the configured temperature value.

The default temperature (0.3) is unchanged. Python engine only.

#### `max_tokens_per_agent` default lowered from 32768 to 16384; out-of-range values are now clamped (closes #357)

**Behavior change**: the default output-token budget per agent call is now **16384** (previously 32768 in the Python engine, 8192 in the bash engine — docs were inconsistent with code). If you relied on the Python engine's prior 32768 default, set `max-tokens-per-agent: 32768` (or `AI_MAX_TOKENS_PER_AGENT=32768`) to restore the previous budget.

Out-of-range values are now clamped at config load time: values below 256 are raised to 256 and values above 65536 are lowered to 65536, each with a `WARNING` printed to stderr. This aligns the runtime with the `[256–65536]` range documented in the reference docs.

#### Native analyzer wrappers now run concurrently (closes #354)

Static analyzer subprocess wrappers (`shellcheck`, `trufflehog`, `semgrep`, `ruff`, and others) previously ran sequentially — each wrapper blocked until the previous one finished. On repos where several analyzers are eligible, this added 2–5× the latency of the slowest single wrapper.

Analyzers now run concurrently via `anyio.to_thread.run_sync` under a shared `CapacityLimiter`. The new `AI_ANALYZER_CONCURRENCY` env var (and `analyzer-concurrency` action input) sets the cap (default 4). Setting `AI_PARALLEL=false` forces the cap to 1 (sequential, matching the old behavior). Results are returned in the original analyzer-list order for deterministic golden-fixture comparison. A single analyzer crash produces a warning and an empty slot — the remaining analyzers proceed normally.

Python engine only.

#### Native wrappers for ruff, semgrep, and hadolint are skipped when equivalent SARIF is supplied (closes #353)

If `AI_SARIF_PATHS` includes a SARIF file whose filename stem matches `ruff`, `semgrep`, or `hadolint` (case-insensitive), the corresponding native wrapper is not run. A `[ai-pr-review] INFO` line is printed for each skipped analyzer. When `AI_SARIF_PATHS` is empty, behavior is unchanged. The match is purely on filename stem — no JSON parsing of the SARIF file is required, so a malformed path simply produces no skip (fail-soft). Add `ruff.sarif`, `semgrep.sarif`, or `hadolint.sarif` to your `AI_SARIF_PATHS` to enable.

Python engine only.

## [1.2.0] - 2026-06-05

### Added

#### Diff-scope severity cap for native analyzer findings (PR #444, closes #359)

Native static analyzers (phpcs, phpstan, ruff, golangci-lint, semgrep, and others) lint entire files — a single changed line in a large legacy file can produce hundreds of diagnostics on unchanged code, flooding the review and triggering `REQUEST_CHANGES` for pre-existing issues.

The new `analyzer-diff-scope` input (or `AI_ANALYZER_DIFF_SCOPE` env var) controls how findings outside the changed lines are handled:

- `cap` (default): findings outside the diff are downgraded to Low severity and marked `out_of_diff=True`. They are collapsed into a `<details>` section in the review body — visible but never trigger `REQUEST_CHANGES`.
- `drop`: out-of-diff analyzer findings are removed entirely.
- `off`: pass through unchanged (full-file linting behavior, pre-v1.2 default).

LLM-agent findings are never affected regardless of this setting. Python engine only. A rollup pass collapses findings where the same rule fires more than 5 times in one file into a single entry with an occurrence count and line list.

### Fixed

#### `exclude-patterns-mode` now validates input and normalizes case (PR #443, closes #442)

The `exclude-patterns-mode` input (and `AI_EXCLUDE_PATTERNS_MODE` env var) previously accepted any string, silently falling through to `append` behavior on typos like `replaces` or `add`. It now validates that the value is `append` or `replace`, raising a `ValueError` at startup for any other value. Values are case-insensitive and normalized to lowercase, so `APPEND`, `Replace`, etc. are accepted. Python engine only.

## [1.1.0] - 2026-06-05

### Added

#### Config-driven diff exclude patterns (PR #438, closes #436)

The diff exclude list is now configurable. Use the new `exclude-patterns` action input (or `AI_EXCLUDE_PATTERNS` env var) to supply comma-separated git pathspec glob patterns that are excluded from the diff before the LLM reads them — reducing token costs directly on repos with large generated, documentation-only, or vendored trees. The `":!"` pathspec prefix is added automatically. Entries are split on commas and surrounding whitespace is trimmed, so `vendor/*, generated/*` is treated the same as `vendor/*,generated/*`. Python engine only.

The `exclude-patterns-mode` input (or `AI_EXCLUDE_PATTERNS_MODE` env var) controls how user-supplied patterns combine with the built-in lockfile/`vendor/`/`node_modules/` excludes. Default `append` adds user patterns after the built-ins; `replace` uses only the user-supplied list.

#### Line-range suppression rules (PR #439, closes #437)

Suppression rules now support `match.line_start` and `match.line_end` fields, scoping a rule to a specific line window within a file. This resolves the granularity gap for repos that vendor upstream code and apply patches: a rule can now target only the upstream line window (e.g. lines 1–200) so that findings on the user's own patched lines (201+) are never silenced. Multi-line findings match on overlap. A finding with no line number is never matched by a range rule. Python engine only.

## [1.0.2] - 2026-06-04

### Fixed

#### `slash-commands.yml` failed to parse as YAML (#434)

A `cat > file <<'PYEOF'` bash heredoc placed Python source at column 0
inside a `run: |` block scalar. YAML's scanner treats lines with less
indentation than the block's established level as document-level keys,
producing a `ScannerError`. Every consumer of `slash-commands.yml` received
a "workflow file issue" failure with 0 jobs — all `/ai-pr-review` slash
commands were broken.

Fix: the Python script is now defined as a `PY_SCRIPT` env var using a YAML
literal block scalar (`|`), and the heredoc is replaced with
`printf '%s' "$PY_SCRIPT" > "$py_script"`. Python logic is unchanged.

#### Feedback loop context extraction gated on wrong event type (#429)

`slash-commands.yml` gated the `feedback-command` job's context-extraction
step on `is_review_comment == 'true'`. Top-level PR comments (the primary
surface for slash commands like `/ai-pr-review dismiss`) always produced empty
`source`, `file`, and `rule_id` fields in the feedback store. The gate is
removed so context extraction runs for all comment types.

#### Finding agents could lose all findings on large diffs (#430, #432)

All seven finding agents (`code-reviewer`, `silent-failure-hunter`,
`architecture-reviewer`, `security-reviewer`, `blind-hunter`,
`edge-case-hunter`, `adversarial-general`) were instructed to emit the full
markdown analysis first and the structured `json-findings` block last. On
large PRs, `stop_reason=max_tokens` was reached before the findings block,
producing a `WARNING: … truncated before json-findings block; findings lost`
and zero structured findings recorded from that agent — silent data loss.

Two compounding root causes fixed:

1. **Prompt reorder** (`prompts/_trailer-findings.md` + 6 base prompts):
   agents now emit the `json-findings` block **before** the markdown report,
   so truncation cuts trailing prose instead of structured findings.

2. **Token budget** (`config.py`, `roster.py`): `AI_MAX_TOKENS_PER_AGENT`
   default raised from 8192 to 16384 (the prior default was silently halving
   the per-agent roster budget). Prose-heavy finding agents raised from 16384
   to 32768 in the roster. `issue-linker` and `pr-summarizer` unchanged.

Python test coverage added for the previously untested truncation paths in
`extract.py` (`_try_repair` salvage and no-fence truncation warning).

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
