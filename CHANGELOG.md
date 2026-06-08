# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

#### issue-linker now pre-fetches the open-issue list via `gh issue list` (closes #446)

The issue-linker agent previously emitted raw `<tool_call>` XML when its prompt instructed the model to run `gh issue list` — a command the text-completion call path cannot execute.

The fix is deterministic: Python now runs `gh issue list --state open --limit 50` via subprocess **before** the LLM call and injects the result as a plain-text `## Open Issues` block in the user message. The model never calls any tools; it assesses the pre-fetched list the same way it already assessed the commit log. The `gh` CLI is already installed in the container image and `GH_TOKEN` is already set at runtime, so no new permissions or secrets are required (the `issues: write` permission in the example workflows implies the `read` access needed here).

Behaviour changes:
- The `### Linked Issues` table now fills in real issue titles when the referenced `#N` appears in the open list (rather than "`title not available`").
- The `### Potentially Related` section now surfaces real open issues by number and title when their title or labels match extracted keywords — no fabricated `#` references.
- The fetch is fail-soft: if `gh` is absent, times out, or returns an error, the section falls back to `(unavailable)` and analysis continues from the commit log and manifest alone.

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
