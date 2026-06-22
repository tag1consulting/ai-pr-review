# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] - 2026-06-22

### Added

- `feat(judge)`: LLM judge pass (Phase 2.75) — after merge/suppress/scope, one cheap-model call scores candidate findings and down-ranks weak single-source results by lowering confidence and routing them to the review body; corroborated findings (static analyzer + LLM agreement) are exempt from down-ranking; enabled by default (`AI_JUDGE_PASS=true`); always fail-soft (any judge error returns findings unchanged). **Note:** this adds one cheap-model LLM call per review for all `@main`/`@v2` consumers. Set `AI_JUDGE_PASS: false` to restore pre-v2.1 behavior. (#360)

- `feat(routing)`: per-agent language-profile section routing — each agent receives only profile sections relevant to its review focus (`security`, `bugs`, `edge`, `idioms`, `general`), packed under a configurable `AI_PROFILE_MAX_TOKENS` budget (default 4096); the token table gains a "Language profiles" supplementary row for visibility (#355)

### Improved

- `improve(prompts)`: align security-reviewer prompt with Anthropic security-guidance plugin checklist, adding: SSRF, LLM prompt injection, gate/action field mismatch, IaC omitted-arg (Terraform/Pulumi/CDK), GitHub Actions `pull_request_target`/`workflow_dispatch` trust, XXE via Python stdlib XML parsers, DOM XSS sinks (`outerHTML`, `insertAdjacentHTML`, `document.write`), AES ECB mode, Node.js `createCipher`/`createDecipher`, Go shell-invocation pattern, extended Python deserialization (`marshal`, `shelve`, `joblib`, `pandas.read_pickle`, `numpy allow_pickle`), ML model unsafe loading (`torch.load` without `weights_only=True`), missing SRI on external scripts, GitHub Actions workflow injection via untrusted context expressions, and parser/validator differential analysis (#369)

## [2.0.0] - 2026-06-22

### Removed (breaking)

- **Bash engine deleted** (closes #199, #251, #252, #253, #254, #255, #257, #258): The bash orchestrator and all supporting shell code have been removed. Python has been the default engine since v1.0.0 (Epic 4, 2026-06-02); this release completes the transition.

  Files deleted (~9,600 LOC across 59 shell files):
  - `review.sh` (main bash orchestrator)
  - `llm-call.sh` (bash LLM client)
  - `lib/agents.sh`, `lib/diff.sh`, `lib/finding-ids.sh`, `lib/findings.sh`, `lib/languages.sh`, `lib/pricing.sh`
  - `vcs/common.sh`
  - `post-review.sh`, `post-review-gitlab.sh`, `post-review-bitbucket.sh`
  - All 13 `analyzers/run-*.sh` wrappers (now superseded by the native Python implementations in `ai_pr_review/analyzers/native/`, shipped in v1.4.0)
  - `tests/*.bats` (33 bats test files) and `tests/test_helper.bash`
  - `docs/analyzers-bash-inventory.md`

  CI change: the `shellcheck` and `test` (bats) jobs in `.github/workflows/lint.yml` have been removed; pytest is the sole test runner.

  Container image: `jq` removed from the runtime image (was used only by bash scripts); the image ENTRYPOINT is now `python3 -m ai_pr_review review`. Asset directories (`prompts/`, `language-profiles/`, `config/`) continue to be mounted at `/opt/ai-pr-review/` via `ENV AI_PR_REVIEW_SCRIPT_DIR=/opt/ai-pr-review`.

- **`AI_PR_REVIEW_ENGINE` environment variable deprecated**: The internal engine-selection variable has been removed from `ai_pr_review/config.py`. Setting it now emits a deprecation warning and is otherwise ignored. The `engine` action input (in both `action.yml` and `container-action/action.yml`) is retained as a **deprecated no-op** for backward compatibility — workflows with `engine: python` or `engine: bash` continue to work; the value is accepted and ignored.

### Migration guide

**Container-action consumers (`uses: ./container-action` or the ghcr.io image directly) require no changes.** The Python package is pre-installed in the container image and the new ENTRYPOINT is set automatically.

**Composite/direct-action consumers (`uses: tag1consulting/ai-pr-review@main`):** The composite action now includes an explicit `pip install` step that installs the `ai_pr_review` package onto the runner before invoking it. This step was absent before v2.0.0, making the composite action non-functional on any runner where the package was not already installed. In practice, consumers using the composite action should see no behavior change — the install step is additive — but if your workflow caches pip packages, you may need to invalidate that cache to pick up this new install step.

**`engine` input:** still accepted; now a no-op. `engine: python` and `engine: bash` both silently run Python. No action required.

**Bitbucket Pipelines container users:** Replace `/opt/ai-pr-review/review.sh` with `python3 -m ai_pr_review review`.

**`AI_PR_REVIEW_ENGINE` env var:** Setting this now emits a deprecation warning and is otherwise ignored. It is safe to leave it in existing scripts during rollout; remove it when convenient.

## [1.6.1] - 2026-06-10

### Added

- **Unified single-file workflow template** (#518): `examples/workflows/pr-review.yml` now wires both automatic PR review and slash commands (`/ai-pr-review rescan`, `dismiss`, `review-full`, etc.) in a single file. Consumers copy one file to `.github/workflows/ai-pr-review.yml` instead of two. The `review` job fires on `pull_request` events; the `slash-commands` job fires on `issue_comment` and `pull_request_review_comment` events, with per-job `if:` gating so exactly one job runs per event. The two-file setup (`pr-review.yml` + `comment-triggers.yml`) remains fully supported — `comment-triggers.yml` is kept at its existing path with no breaking changes, and the reusable `slash-commands.yml` workflow contract is unchanged. Secret-name backward compatibility is preserved via `${{ secrets.AI_REVIEW_API_KEY || secrets.ANTHROPIC_API_KEY }}`. This repo's own dogfood workflow (`ai-review.yml`) was also updated to the single-file pattern, replacing the separate `ai-review-commands.yml`.

### Fixed

- **Forward analyzer/agent filters through the slash-command rescan path** (#516): `/ai-pr-review rescan` and `review-full` now accept and forward the `analyzers`, `exclude-analyzers`, `agents`, and `exclude-agents` inputs to the review container, matching the behavior of the main PR-triggered review. Previously a manual rescan re-ran every eligible analyzer/agent regardless of what the calling workflow had excluded. The consumer example wraps each input in an optional `vars.AI_REVIEW_*` repository variable (`AI_REVIEW_ANALYZERS`, `AI_REVIEW_EXCLUDE_ANALYZERS`, `AI_REVIEW_AGENTS`, `AI_REVIEW_EXCLUDE_AGENTS`) so a project can configure filtering once in Settings and have both the main review and all rescan paths honor it.

## [1.6.0] - 2026-06-10

### Added

- **Analyzer and agent allowlist/denylist selection** (#514): Four new action inputs let consumers control which static analyzers and LLM review agents run on each PR.
  - `analyzers` (allowlist) — run only the listed analyzers; `exclude-analyzers` is ignored when set.
  - `exclude-analyzers` (denylist) — skip the listed analyzers; all others run.
  - `agents` (allowlist) — run only the listed agents; `exclude-agents` is ignored when set.
  - `exclude-agents` (denylist) — skip the listed agents; all others run.

  All four inputs are comma-separated and whitespace-trimmed. Empty (the default) means no filtering — full backward compatibility. When the allowlist is non-empty, the denylist is ignored (allowlist takes precedence). For agents, existing gates (`full_mode_only`, conditional triggers) still apply on top of the allowlist; the allowlist narrows the candidate set but never force-runs a gated agent. Excluding `pr-summarizer` suppresses the PR summary comment entirely. Unknown names are rejected at config load with a nearest-match suggestion. Python engine only (same treatment as `exclude-patterns` and `sarif-paths`). Corresponding env vars: `AI_ANALYZERS`, `AI_EXCLUDE_ANALYZERS`, `AI_AGENTS`, `AI_EXCLUDE_AGENTS`.

## [1.5.0] - 2026-06-09

### Performance

- **Pre-compile suppression regexes** (#507): `SuppressionRule.match_file` and `match_pattern` are now compiled once in `_parse_rule()` and stored as `re.Pattern` fields. The per-finding/per-rule `re.compile()` calls in `_rule_matches()` are eliminated; a fallback compile path is retained for rules constructed directly (e.g. in tests).

- **Load shared prompt fragments once per run** (#507): `_governance.md`, `_knowledge-cutoff.md`, `_trailer-findings.md`, and `suggestion-addendum.md` are loaded once in `build_review_runtime()` via the new `load_shared_prompt_fragments()` helper, stored in a `_SharedPromptFragments` dataclass, and threaded through `DispatchContext`. `effective_prompt()` uses the pre-loaded bytes; disk reads only occur as a fallback when the context is constructed without them (e.g. in tests).

- **Single-pass unified diff parse** (#507): `parse_diff_sets()` in `diff/linemap.py` returns both the added-line set and the new-file set in a single pass over the diff text. `github.py` and `gitlab.py` now call it once instead of calling `parse_added_lines()` and `parse_new_file_lines()` separately.

- **Hoist manifest IaC regexes to module level** (#507): The IaC heuristic regex (`k8s|kubernetes|helm|...`) and the `Dockerfile.` variant pattern previously compiled inside the per-file loop in `manifest.py` are now module-level constants alongside `_CONFIG_PATTERN` and `_DOC_PATTERN`.

- **Hoist tree-sitter parse to once per tier** (#506, closes #499): `_compute_context_enrichment_block()` now runs once per tier in `run_tier()` and the result is threaded to each agent, eliminating per-agent re-parsing of the diff for symbol extraction.

- **Gate language profile on `context_enrichment_eligible`** (#509, closes #501 item 9): `language_profile_text` is no longer injected into the `system_prefix` of agents with `context_enrichment_eligible=False` (e.g. `blind-hunter`). `blind-hunter`'s prompt instructs the model to ignore all project context; the profile was sending tokens the model was told to discard. `feedback_addendum` continues to reach all agents.

### Fixed

- **Skip comments now upsert instead of always posting new** (#511, closes #501 item 5): `post_skip_comment()` in all three VCS providers (GitHub, GitLab, Bitbucket) now mirrors the `post_summary()` upsert pattern: list existing skip comments by the new `SKIP_MARKER`, PATCH/PUT the first, delete duplicates, and only POST when none exist. A new `SKIP_MARKER` constant (`<!-- ai-pr-review-skip -->`) in `marker.py` serves as the upsert anchor, distinct from `INLINE_MARKER` and `SUMMARY_MARKER_PREFIX`.

- **Drop self-refuting findings** (#505, closes #504): The code-reviewer agent was occasionally emitting findings it then immediately refuted in the same response. These are now detected and dropped before posting.

- **SHA watermark advance deferred until post_findings succeeds** (#496, closes #493): The incremental-review watermark is no longer advanced when posting findings fails, preventing a stale SHA from skipping re-review of unposted findings on the next run.

- **Argument injection via attacker-controlled filenames** (#492): Filenames from the diff are now passed to subprocess calls via safe argument arrays rather than shell interpolation.

- **TruffleHog allowlist hardening** (#492): Allowlist entries are validated and shell-escaped.

- **HTML defanging in display output** (#492): `<`, `>`, and `&` in finding text displayed in GitHub reviews are now defanged to prevent XSS in GitHub's markdown renderer.

- **Local catch-all suppressions rejected** (#495, closes #491): `.ai-pr-review-suppressions.yml` entries that match all files (`match_file: "."` or equivalent) are now rejected with an error; only the global suppression file may carry catch-all rules.

### Changed

- **Provider API key export scoped to matching provider** (#510, closes #501 item 11): `action.yml` now validates `inputs.provider` against the known set (`anthropic`, `openai`, `openai-compatible`, `google`, `bedrock-proxy`) in a dedicated bash step and exports only the matching `*_API_KEY` environment variable. Unknown provider values produce a clear error at workflow time. Previously all four provider key vars were unconditionally set.

- **Shared system prefix for run-shared content** (#503): `feedback_addendum` and `language_profile_text` are placed in `LLMRequest.system_prefix` rather than appended to `system_prompt`, enabling Anthropic and Bedrock multi-breakpoint prompt caching to treat them as run-shared and cache them once across all agents in a run.

- **Lockfile scanning and CVE check improvements** (#490): `cve-check` now parses `poetry.lock` and `uv.lock` with a shared helper; prefers exact package versions over range manifests for more accurate OSV lookups.

### Documentation / consistency

- **`context-enrichment` default split documented** (#508, closes #501 item 6): Both `action.yml` (default `false`) and `container-action/action.yml` (default `true`) now carry explanatory comments documenting why the defaults differ and that the Python engine no-ops gracefully when tree-sitter or ripgrep is absent.

- **`eslint` logs a warning when no config found** (#508, closes #501 item 7): `_run_eslint()` now logs `WARNING: no eslint config found; skipping` instead of silently returning `[]`, matching the behavior of all other native analyzers.

- **`cve_check` timeout constant documented** (#508, closes #501 item 8): A comment above `_HTTP_TIMEOUT = 10.0` in `cve_check.py` explains why the name intentionally differs from the `_TIMEOUT_SECS = 120` convention used by subprocess-bound sibling analyzers.

- **Python engine module map added to CLAUDE.md** (#502): Internal architecture documentation for contributors.

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
