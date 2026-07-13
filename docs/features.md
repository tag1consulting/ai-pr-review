---
layout: default
title: Features
nav_order: 3
render_with_liquid: false
---

# Features

## What's new in v2.4.3

**`code-reviewer` no longer flags the `pr-number`/`issue-number` split-input pattern as broken.** Workflows that route both `issue_comment` and `pull_request_review_comment` slash-command events through one reusable workflow correctly fall back to a sibling `issue-number` input when `github.event.pull_request` doesn't exist on the `issue_comment` payload — this is the intended pattern, not a bug. Fixed with a prompt-level constraint plus a deterministic suppression backstop, the same approach used for knowledge-cutoff false positives.

**Two self-action-pin suppression rules that never actually fired are now fixed.** One's file match didn't cover the documented consumer filename; the other's text pattern could never match a semgrep-sourced finding, since semgrep findings are built from the rule ID and generic message only. The underlying supply-chain rule still fires normally for genuine third-party actions.

**Provider API keys, tokens, and base URLs are now stripped of stray whitespace before use.** A trailing newline in a secret (easy to introduce via `echo "$KEY" | gh secret set ...`) previously produced a confusing `Illegal header value` error instead of a clear setup message.

## What's new in v2.4.2

**`false-positive`/`wont-fix` now dismiss the owning review, matching `dismiss`.** Previously, replying `false-positive` or `wont-fix` to an inline finding fell through to a resolve-only step that never touched the owning `CHANGES_REQUESTED` review; only `dismiss` did. Both commands now trigger the same review-dismissal path.

**Clearing the last active finding across every bot review now auto-approves the PR.** GitHub's REST API has no review-state-conversion endpoint, so a dismiss/false-positive/wont-fix that cleared the last finding could leave `reviewDecision` stuck at `REVIEW_REQUIRED` even with zero outstanding findings. A new PR-wide check (`_approve_if_pr_fully_resolved()`) dismisses every clear bot `CHANGES_REQUESTED` review and submits a fresh `APPROVE` once none have unresolved findings left. Gated one tier stricter than plain dismiss (requires `OWNER`/`MEMBER` author association, not `COLLABORATOR`).

## What's new in v2.4.1

**Fixed a crash on demanding diffs: Claude Sonnet 5's adaptive thinking could exhaust `max_tokens` before producing any text**, taking down `code-reviewer` and `silent-failure-hunter` with no review output at all. Sonnet 5's thinking effort is now capped via `output_config: {"effort": "low"}` on affected models.

**Live-API model canary added.** A new scheduled workflow runs the real dispatch path against a genuinely demanding diff for every model with a live API key configured, asserting each call completes with `stop_reason: end_turn` rather than a silent truncation. Catches the next model-behavior surprise before it reaches production.

**Telemetry schema v3.** Per-agent telemetry events now include `stop_reason` and `thinking_tokens`. Consumers reading the telemetry sink should account for the schema version bump from v2.

## What's new in v2.4.0

**Default Anthropic and Bedrock-proxy standard model bumped to Sonnet 5.** `claude-sonnet-5` / `us.anthropic.claude-sonnet-5` supersedes Sonnet 4.6 as the default standard-tier model; premium (Opus) defaults are unchanged. Pricing and temperature handling were updated alongside the default.

**Category-aware dedup, and category mapping for all 13 static analyzers.** The `category` taxonomy introduced in v2.3.1 now drives `findings/merge.py`'s clustering logic — a finding can't join a cluster that already has a conflicting real category, and corroboration between an LLM agent and a static analyzer now requires category agreement. All 13 native static analyzers (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, eslint, phpstan, kube-linter, tflint, cve-check) now map their own findings onto the same taxonomy instead of reporting `"other"` unconditionally.

**Semgrep's category-mapping heuristic hardened against false positives.** Check-ID hint fragments are now matched as delimiter-bounded whole tokens instead of bare substrings, fixing mis-tagged findings like `python.lang.sqlite-config` (previously mis-tagged `injection` via the `"sqli"` fragment matching inside `"sqlite"`).

## What's new in v2.3.1

**`category` field added to the shared `json-findings` schema.** Findings now carry an 11-value taxonomy (`authz`, `injection`, `dependency-cve`, `secret`, `architecture-coupling`, `test-gap`, `edge-case`, `observability`, `docs`, `lint`, `other`), ported from `claude-comprehensive-review#76`. An unrecognized or missing value normalises to `"other"` rather than dropping the finding. This release only adds the field to the schema, the `Finding` model, and the 5 agent prompts that emit inline findings; it does not yet change dedup/clustering behavior, and static-analyzer findings (semgrep, trufflehog, etc.) still report `"other"` since only LLM-agent prompts were updated.

**`actions-token` secret in `slash-commands.yml` is now optional.** A missing required secret on a reusable-workflow call previously failed at run-graph setup before any job-level `if:` gate could evaluate, so a consumer without `actions-token` in its `secrets:` block got a `startup_failure` on every issue comment, not just `/ai-pr-review` commands. The callee now falls back to its own `github.token` when the caller omits `actions-token`; passing it explicitly is still recommended.

## What's new in v2.3.0

**Slash-command dismiss orchestration ported from workflow-embedded bash to the Python engine.** `/ai-pr-review dismiss`, `false-positive`, and `wont-fix` now run through a new `ai_pr_review/slash/dismiss.py` module and CLI subcommands (`dismiss`, `dismiss-inline`, `feedback-context`, `resolve-thread`), replacing ~1,100 lines of inline bash and GraphQL calls in `slash-commands.yml`. Finding classification (body vs. inline) is pytest-covered rather than a bash string match. User-facing command syntax is unchanged.

**Two dismiss bugs fixed as part of the port.** `/ai-pr-review dismiss` previously failed to locate out-of-diff findings because a body-scan filter dropped reviews that render findings in a different heading structure. Separately, the dismiss helper could attempt a dismiss PUT against a review that was already dismissed, approved, or commented, surfacing a confusing warning instead of a clean no-op; it now checks the review's current state first.

## What's new in v2.2.2

**Stale `CHANGES_REQUESTED` review no longer persists after a zero-finding rescan.** When a re-review produced no findings, the bot posted an `APPROVE` review but the prior `CHANGES_REQUESTED` review was never dismissed, permanently blocking the PR. `resolve_stale` now dismisses only CR reviews that are not the current run's.

**`jq` restored to the container image**, fixing silent failures in `feedback-command`'s `wont-fix`/`false-positive` steps. Base image bumped to `ubuntu:26.04` (Python 3.14).

## What's new in v2.2.1

**CLI decomposition and VCS dedup (no behavior change).** Preflight LLM agent code and post-review output formatting were extracted out of `cli.py` into dedicated modules; GitHub and GitLab inline/body finding partitioning now shares a single helper. **kube-linter and phpstan severity mapping fixed**: 15 security-sensitive kube-linter checks and phpstan finding levels now map to accurate severities instead of a flat `Medium`.

## What's new in v2.2.0

**`AI_FAIL_ON_FINDINGS` CI gate** (`fail-on-findings` action input): exit code 2 when the review outcome is `REQUEST_CHANGES` or `COMMENT`, so branch protection can block merge until the bot approves.

**`AI_CONTEXT_MAX_QUERIES` raised from 50 to 200** — the previous hardcoded cap on ripgrep symbol-lookup queries was shared across all agents in a run and was often exhausted before every agent got context enrichment.

**Unknown `AI_*` variables now warn instead of aborting the review**, so pinning an older container image against a newer action no longer breaks on a variable the image doesn't recognize.

## What's new in v2.1.1

**Judge `max_tokens` raised from 1024 to 4096 (silent regression fix).** The judge's JSON response was being truncated on PRs with more than approximately 20 findings, causing parse failure and fail-soft fallback. The result: every moderate-to-large review since v2.1.0 was running with the judge effectively disabled, silently. No configuration change is needed; the fix is automatic.

**Skip-path crash fixed.** The skip path (invoked when the diff is empty or the PR is in draft mode) constructed a `DispatchContext` using `config.model_standard` before `resolve_models()` had been called, leaving `model_standard` empty and causing a crash on any skip-eligible PR. Fixed in `cli.py` by passing `config.resolve_models()` to `_orchestrate_skip()`.

**Judge-pass token usage now visible in the token table.** The judge LLM call now appears as a `judge-pass` row in the token table (see [Token usage table](#token-usage-table)), with its input and output token counts included in the Total row. The row appears only when the judge actually ran (non-empty input and at least one token consumed).

## What's new in v2.1.0

**LLM judge pass (Phase 2.75, `AI_JUDGE_PASS=true`).** After findings are extracted, merged, suppressed, and scoped, a single cheap-model call scores each candidate finding and may `downrank` weak single-source results: confidence is lowered by 15 points and the finding is routed to the review body instead of as an inline comment, so it is still reported but does not trigger a `REQUEST_CHANGES` outcome. `keep` verdicts leave findings unchanged. Corroborated findings (static-analyzer + LLM-agent agreement on the same file/line) are exempt from downranking regardless of the judge's verdict. The judge is always fail-soft — any error returns findings unchanged. Enabled by default; set `AI_JUDGE_PASS=false` to restore pre-v2.1 behavior.

**Per-agent language-profile section routing (`AI_PROFILE_MAX_TOKENS=4096`).** Each agent now receives only the language-profile sections relevant to its review focus, packed under a configurable token budget: `security-reviewer` gets only security sections; `silent-failure-hunter` and `edge-case-hunter` get bug/edge-case sections; broad agents (`code-reviewer`, `architecture-reviewer`, `adversarial-general`) get all sections. The token table gains a "Language profiles" supplementary row showing total profile tokens injected.

**Security-reviewer prompt aligned with Anthropic security-guidance plugin.** New checks added: SSRF, LLM prompt injection, IaC omitted-argument patterns (Terraform/Pulumi/CDK), GitHub Actions `pull_request_target` trust, XXE via Python stdlib XML parsers, DOM XSS sinks, AES ECB mode, unsafe Python deserialization (`marshal`, `shelve`, `joblib`, `pandas.read_pickle`, `numpy allow_pickle`), ML model unsafe loading (`torch.load` without `weights_only=True`), missing SRI on external scripts, and GitHub Actions workflow injection via untrusted context expressions.

## What's new in v2.0.0

**Bash engine removed; Python is the sole engine (closes #199, #251–#258).** The bash orchestrator and all supporting shell code have been deleted (~9,600 lines across 59 files): `review.sh`, `llm-call.sh`, the `lib/*.sh` helpers, `vcs/common.sh`, the three `post-review*.sh` scripts, all 13 `analyzers/run-*.sh` wrappers (superseded by the native Python analyzers shipped in v1.4.0), the 33-file `.bats` suite, and `docs/analyzers-bash-inventory.md`. Python has been the default engine since v1.0.0; this release completes the transition.

**CI and container changes.** The `shellcheck` and `test` (bats) jobs are removed from `.github/workflows/lint.yml`; pytest is the sole test runner. `jq` is removed from the runtime image (it was used only by the bash scripts), and the image `ENTRYPOINT` is now `python3 -m ai_pr_review review`. Asset directories (`prompts/`, `language-profiles/`, `config/`) continue to mount at `/opt/ai-pr-review/`.

**`AI_PR_REVIEW_ENGINE` deprecated; `engine` input is now a no-op.** Setting the `AI_PR_REVIEW_ENGINE` environment variable emits a deprecation warning and is otherwise ignored. The `engine` action input is retained as a deprecated no-op (`engine: python` and `engine: bash` both run Python silently). Container-action and composite/direct-action consumers need no changes; the composite action now includes an explicit `pip install` step (invalidate any pip cache to pick it up).

## What's new in v1.6.1

**Unified single-file workflow template (PR #518).** `examples/workflows/pr-review.yml` now wires both automatic PR review and slash commands (`/ai-pr-review rescan`, `dismiss`, `review-full`, etc.) in a single file: copy one file to `.github/workflows/ai-pr-review.yml` instead of two. The `review` job fires on `pull_request` events and the `slash-commands` job fires on `issue_comment`/`pull_request_review_comment` events, with per-job `if:` gating so exactly one runs per event. The two-file setup (`pr-review.yml` + `comment-triggers.yml`) remains fully supported with no breaking changes, and secret-name backward compatibility is preserved via `${{ secrets.AI_REVIEW_API_KEY || secrets.ANTHROPIC_API_KEY }}`.

**Analyzer/agent filters forwarded through the slash-command rescan path (PRs #516, #517).** `/ai-pr-review rescan` and `review-full` now accept and forward the `analyzers`, `exclude-analyzers`, `agents`, and `exclude-agents` inputs to the review container, matching the main PR-triggered review. Previously a manual rescan re-ran every eligible analyzer/agent regardless of what the calling workflow excluded. The consumer example wraps each input in an optional `vars.AI_REVIEW_*` repository variable so a project can configure filtering once and have both the main review and all rescan paths honor it.

## What's new in v1.6.0

**Analyzer and agent allowlist/denylist selection (PR #514).** Four new action inputs control which static analyzers and LLM review agents run on each PR: `analyzers` (allowlist) and `exclude-analyzers` (denylist) for static analyzers, and `agents` (allowlist) and `exclude-agents` (denylist) for review agents. All four are comma-separated and whitespace-trimmed; empty (the default) means no filtering. When an allowlist is non-empty its corresponding denylist is ignored (allowlist takes precedence). Existing agent gates (`full_mode_only`, conditional triggers) still apply on top of the allowlist; it narrows the candidate set but never force-runs a gated agent. Excluding `pr-summarizer` suppresses the PR summary comment entirely. Unknown names are rejected at config load with a nearest-match suggestion. Corresponding env vars: `AI_ANALYZERS`, `AI_EXCLUDE_ANALYZERS`, `AI_AGENTS`, `AI_EXCLUDE_AGENTS`. See [Configuration](configuration). Python engine only.

## What's new in v1.5.0

**Performance pass (PR #507, #506, #509).** Suppression-rule regexes are pre-compiled once in `_parse_rule()` rather than on every finding/rule check. Shared prompt fragments (`_governance.md`, `_knowledge-cutoff.md`, `_trailer-findings.md`, `suggestion-addendum.md`) are loaded once per run and threaded through `DispatchContext` instead of re-read per agent. The unified diff is parsed in a single pass (`parse_diff_sets()` returns both the added-line set and new-file set at once). Tree-sitter context enrichment is computed once per tier instead of once per agent, and the language profile is no longer injected into agents that ignore project context (e.g. `blind-hunter`).

**Security hardening (PR #492, #495).** Filenames from the diff are passed to subprocess calls via safe argument arrays rather than shell interpolation, closing an argument-injection vector via attacker-controlled filenames. TruffleHog allowlist entries are validated and shell-escaped. `<`, `>`, and `&` in finding text are defanged to prevent XSS in GitHub's markdown renderer. Local `.ai-pr-review-suppressions.yml` files may no longer carry catch-all (match-all-files) rules; only the global suppression file may.

**Correctness fixes (PR #511, #505, #496, #510).** Skip comments now upsert in place (via a new `<!-- ai-pr-review-skip -->` marker) instead of always posting a fresh comment, mirroring the summary-comment pattern across GitHub, GitLab, and Bitbucket. Findings the code-reviewer agent immediately refutes in the same response are dropped before posting. The incremental-review SHA watermark is no longer advanced when posting findings fails. `action.yml` exports only the `*_API_KEY` matching the configured `provider`; unknown provider values fail fast. `cve-check` now parses `poetry.lock` and `uv.lock` and prefers exact package versions for more accurate OSV lookups.

## What's new in v1.4.0

**All 13 static analyzers ported to native Python (Epic 8, PRs #475–#488, closes #462–#474).** Every analyzer is implemented as a native Python function in `ai_pr_review/analyzers/native/`. The `analyzers/bridge.py` dispatcher maps each tool name to its Python callable.

Analyzers: shellcheck, ruff, hadolint, kube-linter, phpcs, semgrep, golangci-lint, checkov, phpstan, eslint, tflint, trufflehog, cve-check.

Each tool binary is invoked via `subprocess.run`, with output parsed directly in Python. Eligibility gating (changed-files filtering, content-sniff checks) runs in Python. Each native analyzer has a corresponding `tests/python/test_analyzer_<tool>.py` pytest module.

No action input changes are required.

## What's new in v1.3.0

**Concurrent native analyzer wrappers (PR #454, closes #354).** Native static-analyzer subprocesses (`shellcheck`, `trufflehog`, `semgrep`, `ruff`, and others) previously ran sequentially. They now run concurrently via `anyio.to_thread.run_sync` under a shared `CapacityLimiter`. Use the new `analyzer-concurrency` action input (or `AI_ANALYZER_CONCURRENCY` env var) to control the cap (default 4). Setting `parallel: false` forces the cap to 1 (restoring sequential behavior). Results are always returned in the original analyzer-list order for deterministic output. See [Configuration → analyzer-concurrency](configuration#static-analyzer-options).

**SARIF-equivalent skip for native wrappers (PR #454, closes #353).** When `AI_SARIF_PATHS` includes a SARIF file whose filename stem matches `ruff`, `semgrep`, or `hadolint` (case-insensitive), the corresponding native wrapper is suppressed and an INFO log is printed. This avoids running the same analyzer twice when you supply SARIF output from your own CI step. When `AI_SARIF_PATHS` is empty, behavior is unchanged.

**`AI_TEMPERATURE` is now honored in LLM requests (PR #453, closes #356).** The `temperature` action input (and `AI_TEMPERATURE` env var) was already validated but was never forwarded to the underlying LLM provider call. All agents, the pr-summarizer, and the issue-linker now receive the configured temperature. Default is 0.3 (unchanged).

**`max_tokens_per_agent` default lowered and clamped (PR #453, closes #357).** The default output-token budget per agent call is now **16384**. Out-of-range values are clamped at config load time with a `WARNING` to stderr: below 256 is raised to 256, above 65536 is lowered to 65536.

**`ignore-merge-commits` now defaults to `true` (PR #450, closes #448).** Merge commits that pull upstream base-branch changes into a PR are now excluded from the diff by default, so only the PR author's own commits are reviewed. **Breaking change**: if your PRs contain base-branch merge commits and you rely on them appearing in the diff, add `ignore-merge-commits: false` (or `AI_REVIEW_IGNORE_MERGE_COMMITS=false`) to restore the previous behavior.

**Context enrichment now defaults to `true` in the container image (PR #451, closes #391).** The container image ships `tree-sitter-language-pack` and `ripgrep`, so the dependencies required for context enrichment are always present. The `context-enrichment` input now defaults to `true` in `container-action/action.yml`. Direct-action consumers keep the `false` default.

**Issue-linker pre-fetches open issues via `gh issue list` (PR #447, closes #446).** The issue-linker agent now receives a pre-fetched list of open issues injected into its prompt, so it can resolve referenced `#N` numbers to real titles and surface genuinely related issues by keyword matching.

**Slash-command replies now post as `github-actions[bot]` (PR #452).** Slash-command and learning-loop replies previously posted as the PAT owner. They now post as `github-actions[bot]`. Callers of `slash-commands.yml` must add `actions-token: ${{ secrets.GITHUB_TOKEN }}` to the `secrets:` block. See [Slash commands](slash-commands.md) for the updated configuration.

## What's new in v1.2.0

**Diff-scope severity cap for native analyzer findings (PR #444, closes #359).** Native static analyzers (phpcs, phpstan, ruff, golangci-lint, semgrep, etc.) lint entire files — a single changed line in a large legacy file can produce hundreds of diagnostics on unchanged code. The new `analyzer-diff-scope` input (or `AI_ANALYZER_DIFF_SCOPE` env var) controls how those out-of-diff findings are handled. `cap` (default): downgrade out-of-diff analyzer findings to Low severity and collapse them into a `<details>` section in the review body — they remain visible but never trigger `REQUEST_CHANGES`. `drop`: remove them entirely. `off`: pass through unchanged (full-file linting behavior, pre-v1.2 default). LLM-agent findings are never affected regardless of this setting. See [Configuration → analyzer-diff-scope](configuration#static-analyzer-options).

**`exclude-patterns-mode` validation (PR #443, closes #442).** The `exclude-patterns-mode` input (and `AI_EXCLUDE_PATTERNS_MODE` env var) now validates that the value is `append` or `replace` — any other value raises an error at startup rather than silently falling through to append behavior. Values are case-insensitive (`APPEND`, `Replace`, etc. are all accepted and normalized to lowercase).

## What's new in v1.1.0

**Config-driven diff exclude patterns (PR #438, closes #436).** The diff exclude list is now configurable. Use the new `exclude-patterns` action input (or `AI_EXCLUDE_PATTERNS` env var) to supply comma-separated git pathspec glob patterns that are excluded from the diff before the LLM reads them — reducing token costs directly on repos with large generated, documentation-only, or vendored trees. The `":!"` pathspec prefix is added automatically. Default mode is `append`, which adds user patterns after the built-in lockfile/`vendor/`/`node_modules/` excludes; set `exclude-patterns-mode: replace` (or `AI_EXCLUDE_PATTERNS_MODE=replace`) to drop the built-ins entirely. See [docs/configuration.md](docs/configuration.md#diff-exclude-patterns).

**Line-range suppression rules (PR #439, closes #437).** Suppression rules now support `match.line_start` and `match.line_end` fields, scoping a rule to a specific line window within a file. This resolves the granularity gap for repos that vendor upstream code and apply patches: a rule can now target only the upstream line window (e.g. lines 1–200) so that findings on the user's own patched lines (201+) are never silenced. Multi-line findings match on overlap. A finding with no line number is never matched by a range rule. See [docs/suppression.md](docs/suppression.md).

## What's new in v1.0.2

**Slash-commands YAML parse fix (PR #434).** The Python heredoc in the slash-commands workflow was moved to an env block scalar, fixing a YAML parse error that prevented the workflow from running in some environments.

**Learning loop feedback context fix (PR #429).** The feedback store now correctly populates `source`, `file`, and `rule_id` fields for `issue_comment` events, so feedback entries written via `/ai-pr-review false-positive` and related commands carry the full context needed for relevance-ranked re-injection.

**Agent output token budget and ordering fix (PR #432).** Agent prompts now emit the `json-findings` block first (before prose explanations), reducing silent truncation on large diffs. The per-agent output budget was subsequently lowered to 16384 in v1.3.0; see the v1.3.0 notes above.

**E2E validation workflow (PR #433).** A Claude Code workflow (`ai-pr-review-e2e`) now builds the container image from the current checkout and runs live reviews against all three test platforms (GitHub, GitLab, Bitbucket) as part of the release process.

## What's new in v1.0.1

**Agent prompt parity with claude-comprehensive-review (PRs #414–#419).** Six agents — `pr-summarizer`, `edge-case-hunter`, `blind-hunter`, `adversarial-general`, `architecture-reviewer`, and `security-reviewer` — received targeted prompt improvements ported from the companion CCR plugin: tighter output structure, improved finding signal-to-noise, and better alignment with the shared language profiles.

**Analyzer correctness fixes (PRs #420–#423).** The semgrep analyzer gains stdin support and ruleset strategy documentation. The cve-check analyzer fixes range version truncation and `requirements.txt` pinning. shellcheck and trufflehog receive correctness improvements. All analyzers accept stdin input via the analyzer bridge.

**Learning loop data quality fix (PR #425).** `source`, `file`, and `rule_id` fields are now correctly populated in `learnings.jsonl` entries, making relevance-ranked feedback injection more accurate.

## What's new in v1.0.0

**Python engine is now the default (issue #249, PR #388).** Consumers who do not set `engine:` automatically use the Python engine starting with this release. Setting `engine: bash` (or `AI_PR_REVIEW_ENGINE=bash`) still works but emits a deprecation warning; the bash pipeline was removed in v2.0.0. A 14-day field soak (80 reviews, zero P0/P1 bugs) preceded the default flip, and Opus 4.8 was set as the Anthropic premium default for full-mode reviews.

## What's new in v0.12.2

**Third-party analyzer license compliance for the container image (PR #376).** The container image redistributes ~15 third-party open-source analyzers; this release adds a `THIRD-PARTY-LICENSES/` directory with each tool's full upstream license text and a `NOTICE.md` manifest (tool, version, license, copyright, corresponding-source URL), bundled into the image at `/opt/ai-pr-review/THIRD-PARTY-LICENSES/` and referenced from the README and architecture docs.

**Semgrep registry rulesets are no longer baked into the image (PR #376).** The Semgrep-maintained `p/ci` and `p/security-audit` rulesets are licensed under the Semgrep Rules License v1.0 (use-restricted; not freely redistributable inside this tool's image), so they are no longer pre-downloaded. `run-semgrep.sh` falls back to `--config=auto`, which fetches rules at runtime instead. **Behavior note:** semgrep scans now require network access at review time and re-incur the ~20–40s ruleset fetch that the bake step previously eliminated. Consumers who need an offline/deterministic ruleset can point `SEMGREP_RULES_DIR` at their own permissively-licensed rule bundle. Semgrep finding output is otherwise unchanged.

**`/ai-pr-review dismiss F<n>` now clears `CHANGES_REQUESTED` for inline findings too (PR #378).** Previously, dismissing an *inline* finding by its `F<n>` ID from a top-level PR comment stored the verdict but left the blocking review in place — it only told the user to go reply on the thread, and even manually resolving the thread did not clear the review (manual resolution does not re-trigger the workflow). Now the command locates the inline thread by its `[F<n>]` token, resolves it, and dismisses the `CHANGES_REQUESTED` review once every bot inline thread is resolved — the same outcome as replying directly on the thread. Thread ownership is gated by the `<!-- ai-pr-review-inline -->` marker rather than author login, which fixes a GraphQL/REST bot-login mismatch (`github-actions` vs `github-actions[bot]`) that would otherwise prevent the thread from being found.

## What's new in v0.12.1

**Removed sequence-diagram generation from `pr-summarizer` (PR #374).** The summarizer no longer produces an optional Mermaid `sequenceDiagram` block. GitHub does not render Mermaid in PR comments (only in `.md` files and PR description bodies), and Bitbucket does not render it at all, so the diagram was rarely seen where reviews are read, while adding LLM cost and prompt complexity. The summarizer continues to emit the summary, PR type, effort estimate, and walkthrough table. This is a behavior change to the summary comment but is not breaking for downstream consumers; no inputs or outputs that callers depend on were removed.

**Serena MCP onboarding config (PR #373).** Adds `.serena/` project onboarding configuration and memories for contributors using the Serena code-navigation MCP server. Purely additive: no runtime code paths change.

## What's new in v0.12.0

**Stable per-PR `F<n>` IDs on all findings, with `/ai-pr-review dismiss F<n>` from top-level PR comments (PRs #365, #366, #367, closes #364).** AI review findings now carry stable, monotonically increasing IDs — `**[F1]**`, `**[F2]**`, etc. — across both inline review-thread comments and body-level findings (those in the `### Findings not attached to specific lines` section). IDs are PR-wide: the same finding keeps its ID across review cycles, new findings get the next unused ID, and dismissed gaps (e.g. no `F2`) signal historical dismissals.

Before this release, `/ai-pr-review dismiss` silently did nothing when posted as a top-level PR comment, and body-level findings had no dismissal path at all. Now:

- `/ai-pr-review dismiss F1` posted as a **top-level PR comment** dismisses a specific body-level finding, records a `FeedbackEntry`, and auto-dismisses the `CHANGES_REQUESTED` review when all inline threads are also resolved.
- `/ai-pr-review dismiss` (no ID) replies with the list of active `F<n>` IDs instead of silently doing nothing — fixing the exact user-visible bug.
- Full parity: `false-positive F<n>`, `wont-fix F<n>`, `explain F<n>`, and `revise F<n>` all accept the same body-finding ID syntax from top-level comments.

The ID map is embedded as a hidden HTML comment in every review body (`<!-- ai-pr-review-id-map: {...} -->`) for stateless reconstruction without a side-channel database. A backward-compatible fallback parses rendered bullet text for pre-marker reviews.

**Incremental-run summary preservation (PR #366).** Incremental review runs (where the summarizer is skipped because the diff base matches the last-reviewed SHA) previously overwrote the existing summary comment with a bare `## AI Review` placeholder. The orchestrator now calls `advance_sha_watermark()` on incremental runs instead of `post_summary()`, preserving the original summary comment body while updating only the watermark SHA.

## What's new in v0.11.0

**Governance posture for LLM reviewers (PR #350).** A new shared prompt partial `prompts/_governance.md` is injected into all seven finding-producing agents (`code-reviewer`, `security-reviewer`, `architecture-reviewer`, `edge-case-hunter`, `blind-hunter`, `adversarial-general`, `silent-failure-hunter`). It encodes three principles: an Asimov-style severity lens (calibrate severity by harm to users/systems, not abstract "code smell"), don't-reinvent-the-wheel detection (flag duplication of existing utilities visible in the diff or manifest), and verify-before-naming with secret redaction (any flag/function/path named in a finding must appear in the supplied diff or manifest, and any secret-shaped value visible in the diff must be replaced with `<secret-redacted>` in finding and remediation text). Always-on, no env var toggle. Composition order is `base → _governance → _knowledge-cutoff → _trailer-findings → (suggestion-addendum)` to preserve prompt-cache locality.

**Telemetry schema v2 (PR #345, issues #242, #243).** The telemetry event payload bumps from schema version `"1"` to `"2"` with six additive fields: `provider`, `model_standard`, `model_premium`, `review_mode`, `is_incremental`, and `failed_agent_latency_ms`. The `outcome` enum gains `"skipped"` and `"dry_run"` values for runs where no agent dispatch occurs. All additions are forward-compatible — v1 consumers ignoring unknown keys continue to work; consumers switching on `telemetry_schema_version` should add v2 to their accepted list.

**GitHub Actions step summary (PR #345).** When `GITHUB_STEP_SUMMARY` is set (always true on GitHub-hosted runners), the Python engine now writes a concise markdown block to the step summary showing review mode, file/language counts, agent roster, findings tally by severity, failed agents, and the token cost table. Same layout as the PR comment, so operators see key metrics at a glance without opening the PR. Fail-soft — write errors are logged at WARNING and the review continues.

**Effective `max_tokens_per_agent` in cost table (PR #345).** When a user overrides the roster default via `AI_MAX_TOKENS_PER_AGENT`, the token cost table's Output column now displays the effective cap (e.g. `80 / 4096`) instead of the roster default (`80 / 16384`). Makes per-run token budgeting transparent.

**Dockerfile Python version centralized (PRs #340, #348, issue #340).** Hardcoded `python3.12` paths are replaced with `${PYTHON_VERSION}` interpolation driven by a single `ARG PYTHON_VERSION=3.12` declared in both build stages. Future Python bumps require changing one default instead of five hardcoded sites. No runtime behavior change for action consumers.

**Dependency updates.** Renovate updates: `ruff` 0.15.13 → 0.15.14 (#346), `ruby` 4.0.4 → 4.0.5 (#347).

## What's new in v0.10.1

**`max_tokens_per_agent` default corrected (PR #337, issue #334).** The Python engine's `config.py` defaulted to 4096 while `action.yml` documented 8192. Both now agree on 8192. Consumers relying on the Python engine default were getting half the intended token budget per agent.

**GitLab stale discussion resolution scoped to bot-owned discussions (PR #338, issue #184).** `resolve_stale_discussions` previously matched any discussion where the bot appeared as _any_ note author, including reply threads started by other users. It now only resolves discussions where the bot is the first-note author and the body contains the `<!-- ai-pr-review-inline -->` inline marker. The marker is appended to all newly posted inline discussions, so existing discussions are unaffected.

**`dismiss_stale_reviews` hardened against silent failures (PR #342, issues #329, #325).** A parse failure on the GitHub reviews API response now emits a warning and returns early instead of silently continuing with an empty review ID list. An empty `newest_review_id` is similarly guarded.

**Observability and correctness improvements (PR #341, issues #327–#333).**
- `ImportError` is no longer swallowed in the feedback loop and analyzer bridge fail-soft blocks — genuine import failures now propagate.
- `_safe_int` is no longer called twice per review ID in `github.py`'s bot review collection loop.
- SARIF load failure log now includes the file paths being loaded.
- Token table renderer failure log now includes `head_sha` for context.
- GitHub stale review dismissal now emits per-review-ID debug/warning log lines.
- Missing `language-profiles/` directory now logs a structured warning pointing to `AI_PR_REVIEW_SCRIPT_DIR` rather than returning silently.

**Language profiles loaded once per run (PR #343, issue #326).** The Python engine previously called `load_language_profiles()` on every agent dispatch. Profiles are now loaded once in `build_review_runtime()` and passed via `DispatchContext.language_profile_text`, eliminating redundant disk reads proportional to agent count.

## What's new in v0.10.0

**Language profiles — 19 languages (PR #322).** Agent prompts now include per-language context blocks for every language detected in the diff. Profiles cover Python, Go, TypeScript, JavaScript, PHP, Shell, Ruby, Rust, Java, C++, Kotlin, Swift, C#, Scala, SQL, Lua, Perl, YAML, and Terraform. Each profile supplies language-specific patterns, common pitfalls, and framework conventions so agents apply targeted checks rather than generic heuristics. Profiles are loaded from `language-profiles/` and injected into the `DispatchContext`.

**Premature review dismissal fix (PR #324, issue #323).** Fixed a race condition where a stale `CHANGES_REQUESTED` review could be auto-dismissed before the current run finished posting its own review. The action now tracks the total bot-review count and always protects the newest bot review from dismissal. A `_safe_int()` helper guards against non-integer review IDs.

**Python engine runtime assembly refactor (PR #321).** `_run_review_async()` in `cli.py` has been reduced from ~230 lines to ~65 lines. A new `build_review_runtime()` factory in `ai_pr_review/review/runtime.py` assembles the fully prepared `ReviewRuntime` dataclass — provider construction, diff computation, feedback loading, agent gate evaluation, static analyzer runs, SARIF ingestion, suppression rule loading, and `OrchestrationConfig` construction — and hands it to `orchestrate.run_review()`, which reads no environment and constructs no dependencies. This makes the Python engine's runtime flow reusable for non-CLI entry points (server harness, batch runner). SARIF findings now flow through `OrchestrationConfig.extra_findings` rather than being loaded inline in the orchestrator.

## What's new in v0.9.4

**Token table moved to review body.** The collapsible **Token usage by agent** table now appears in the same review comment as the findings (Approved / Changes Requested / Comment). The summary comment now carries only the first-run walkthrough and is never overwritten on subsequent pushes.

## What's new in v0.9.3

**Telemetry hooks.** Set `AI_TELEMETRY_ENABLED=true` and `AI_TELEMETRY_SINK=file:///path/to/events.jsonl` (or an `http(s)://` endpoint) to receive one structured JSON event per review run. The event includes outcome, findings counts by severity, per-agent token usage, per-agent wall-clock latency, SARIF elapsed time, and the count of learning-store entries loaded. Telemetry is fail-soft: all I/O errors are logged as warnings and the review continues. See [Configuration → Telemetry hooks](configuration#telemetry-hooks) for the full env-var reference.

**Agent latency tracking.** Each agent now records its wall-clock elapsed time (`elapsed_ms`). The value is included in the telemetry event's `agent_latency_ms` map and is available for future cost-table display.

**Token table enhancements.** The collapsible token cost table now includes two additional optional rows: a **Context enrichment** row showing the token count of the `<symbol-context>` block (when `AI_CONTEXT_ENRICHMENT=1` and context was non-empty), and a **SARIF ingestion** row showing the wall-clock parse time (when `AI_SARIF_PATHS` is configured). The Output column also displays the configured per-agent output cap when one is set (e.g. `1234 / 4096`).

**`cache_priming` default changed to `false`.** `AI_CACHE_PRIMING` now defaults to `false`. If you rely on cache priming, add `AI_CACHE_PRIMING=true` explicitly to your workflow.

**`slash-commands.yml` `shell: bash` fix.** The `feedback-command` job in the bundled slash-commands workflow was missing a `shell: bash` declaration, causing step failures on some runner configurations. This is fixed.

## What's new in v0.9.2

**Token cost table updated on every run (PR #304).** The collapsible token cost table now updates on every incremental run: the first-run PR summary text is preserved and only the `<details>` accordion is replaced with fresh token data from the latest run.

**Token table upsert bug fixes.** Fixed two bugs introduced in v0.9.1: (1) `_upsert_token_table` was a synchronous call inside an async function, blocking the anyio event loop during the GitHub API call; converted to `async def` with `anyio.to_thread.run_sync`. (2) HTTP-level errors from the VCS provider (403, 404, 422) were silently swallowed because providers return `SummaryResult(ok=False)` rather than raising — the return value is now checked and logged as a warning.

**`phpstan_level` default set to 3.** `PHPSTAN_LEVEL` now defaults to `3`.

## What's new in v0.9.1

**Language detection expanded to 23 languages (PR #297).** The action now detects Kotlin, Swift, C#, Scala, Terraform, YAML, SQL, Lua, Perl, plus Drupal PHP extensions (`.module`, `.theme`, `.inc`) and Ruby build files (`.rake`, `.gemspec`). Tree-sitter context enrichment (Capability A) covers all 23 language keys.

**PR summarizer and token cost table wired (PR #299).** On first-run reviews, the action automatically posts a PR summary (walkthrough table, type classification, effort estimate) and a collapsible token cost table. Both are fail-soft: if either fails, review continues and a notice is posted rather than silently omitting output. The token cost table is updated on every run (see v0.9.2 below); the PR summary is posted on first run only.

**Structured logging (PR #300).** Set `AI_LOG_FORMAT=json` to get machine-readable log output with `timestamp`, `level`, `logger`, `correlation_id`, and `message` fields — suitable for Datadog, CloudWatch, and similar aggregators. Correlation IDs flow through every log record for the duration of a review run. Three-layer secret masking prevents credentials from appearing in log output. See [Configuration → Structured logging](configuration#structured-logging) for the full env-var reference.

**Error surface polish (PR #300).** All internal exceptions now use a typed hierarchy (`AiPrReviewError` → `ConfigError` / `ProviderError` / `CapabilityError` / `AnalyzerError` / `EngineError`). Warning messages follow a consistent `[ai-pr-review] WARNING: <component>: <message>` format across all modules.

## What's new in v0.9.0

**Three opt-in capability groups (all default off).** See
[Configuration → Opt-in capabilities](configuration#opt-in-capabilities)
for the full env-var reference.

**Capability A — Context enrichment** (default: `true` in the container image, `false` for direct-action consumers)
- Tree-sitter extracts symbol references from diff hunks (23 language keys), with a regex fallback when tree-sitter is unavailable.
- ripgrep looks up cross-file definitions and ranks by proximity (same-file > same-package > repo-wide).
- Definitions are token-budget-capped and injected into eligible agent prompts as a `<symbol-context>` block. Reduces hallucinated "should check X" findings.

**Capability B — SARIF 2.1.0 ingestion** (`AI_SARIF_PATHS=a.sarif,b.sarif`)
- Parse external scanner output (CodeQL, Semgrep, Trivy, Bandit, custom) into the existing finding pipeline.
- Severity mapping: `error → High`, `warning → Medium`, `note/none → Low`. Source tag: `sarif:<driver.name>`. Confidence: 90.
- Findings flow through the same dedup/suppress/post path as native analyzers. Fail-soft on malformed files.
- See [`examples/workflows/sarif-codeql.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/sarif-codeql.yml) for a CodeQL + AI review pipeline.

**Capability C — Learning loop** (`AI_FEEDBACK_LOOP=true`, GitHub-only)
- New slash commands: `/ai-pr-review false-positive [reason]`, `wont-fix [reason]`, `feedback <text>`, `explain`, `revise <hint>`.
- Verdicts persist to `.ai-pr-review/learnings.jsonl` on a dedicated `ai-pr-review-bot` branch (auto-bootstrapped on first write).
- Future reviews see a `<repo-feedback>` block of relevance-ranked recent entries, so repeated false positives get suppressed without further reviewer action.
- Security-hardened input pipeline: NFC normalization, control-char stripping, length cap, HTML escape, secret-pattern rejection, defensive prompt framing against injection.
- See [Learning loop](learning-loop) for the architecture.

## What's new in v0.7.0

**Performance**
- **Prompt caching** — Anthropic/Bedrock prompt caching via shared-cache layout delivers ~47% cost reduction on average (−46% cold, −61% hot). All agents in a cache cohort share one cache entry per run. (#137, #143)
- **Baked semgrep rulesets** — Container image ships with `p/ci` and `p/security-audit` rulesets pre-downloaded, eliminating the 20-40s network fetch on every run. (#136)
- **Analyzer overhead reduction** — Trufflehog uses batch invocation (single call for all files), checkov has tighter YAML/JSON content sniffing, phpstan avoids a subprocess for Drupal detection. (#134)

**Platform**
- **Multi-arch container images** — `linux/amd64` and `linux/arm64` builds, enabling native ARM runners. (#145)
- **Fork PR support** — Internal workflow uses `pull_request_target` for reviewing fork PRs. (#146)
- **Cache priming (opt-in)** — `AI_CACHE_PRIMING=true` serializes cache-writing calls before parallel fan-out for environments where opportunistic cache hits don't occur. Default off. (#154)

**Quality**
- **Prompt trailer consolidation** — Shared `_knowledge-cutoff.md` and `_trailer-findings.md` files replace duplicated blocks across 7 agent prompts, reducing maintenance surface. (#141)
- **Version hallucination hardening** — `ruby-org` verification type and portable ERE patterns in suppressions. (#140)

**Documentation**
- Comprehensive documentation audit addressing 27+ accuracy findings across README, CLAUDE.md, and the docs site. (#138, #155)

## Code suggestions

Code suggestions are enabled by default. The review tool asks eligible LLM agents to emit concrete code fixes alongside their findings. Each fix is rendered as a ` ```suggestion ` block inside the inline review comment, which GitHub and GitLab display as an "Apply suggestion" button — the PR/MR author can accept the fix with one click.

> **New in v0.6.0:** Suggestions now work on GitLab MRs using GitLab's
> native ` ```suggestion:-N+0 ` syntax for multi-line replacements.
> Previously suggestions were GitHub-only. Requires GitLab 11.6+
> (when the suggestion fence syntax was introduced). The
> `enable-suggestions` flag (`true` by default) applies uniformly
> across all VCS providers — setting it to `false` disables suggestions
> on both GitHub and GitLab. Bitbucket always ignores suggestions
> regardless of this flag.

To disable suggestions, set `enable-suggestions: false`:

```yaml
- uses: tag1consulting/ai-pr-review/container-action@main  # or pin to a release tag
  with:
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
    base-ref: ${{ github.event.pull_request.base.ref }}
    head-sha: ${{ github.event.pull_request.head.sha }}
    enable-suggestions: false
```

**Eligible agents** (those most likely to produce concrete line-level fixes): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`. Design-level agents (`architecture-reviewer`, `adversarial-general`) and static analyzers (shellcheck, semgrep, ruff, etc.) never emit suggestions.

**How it works.** Eligible agents have a short prompt addendum appended to their system prompt instructing them to include a `suggested_code` field (and optional `start_line` for multi-line replacements) only when the fix is concrete and complete. The post-review script constructs the suggestion fence itself — agents are not trusted to emit the markdown directly. Multi-line suggestions are validated against the diff: every line in the replacement range must appear on the new-file side of a diff hunk, or the suggestion is dropped while keeping the natural-language remediation.

**Caveats.** Suggestions increase output token usage. The feature works on both GitHub and GitLab (using GitLab's `suggestion` fence syntax) — Bitbucket reviews ignore it. Suggestions are validated defensively: `start_line` must be a positive integer no greater than `line` with no leading zeros, multi-line ranges are capped at 100 lines, and `suggested_code` containing triple backticks (which would break the suggestion fence) is rejected. When any validation fails, the suggestion is dropped with a WARNING logged to the Actions run and the finding still posts with its natural-language remediation. On incremental reviews (SHA watermark active), suggestions only render when the finding's line range is still in the current incremental diff — add the `ai-review-rescan` label to force a full re-review.

## Incremental reviews

After the first full-PR review, subsequent pushes trigger an incremental review that only analyzes the new commits. The SHA watermark is stored in the summary comment and advanced after each review run.

If the watermark cannot be found (e.g., the summary comment was deleted), the action falls back to a full PR diff.

To force a full-PR diff for a single run, add the **`ai-review-rescan`** label to the PR. The watermark still advances normally afterward, so subsequent pushes resume incremental review — re-add the label if you want another full rescan.

## Resilience

**Graceful agent failure**: If an agent fails (transient API error, content filter block, etc.), the review continues with the remaining agents and notes which agents were skipped. If all finding agents fail, the review is aborted.

**LLM retries**: Transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient network errors (connection refused, timeout) are retried with exponential backoff and jitter. Controlled by the `LLM_RETRY_COUNT` env var (default: 2).

**Parallel execution**: Agents run in a tiered fan-out by default — Tier 1 issues up to ~3 concurrent LLM calls alongside any triggered static analyzers; Tier 2 (full mode only) issues up to 5 concurrent LLM calls. The concurrency numbers apply to LLM calls only (for rate-limit planning); static analyzers run concurrently with them but do not consume LLM quota. If your provider's rate limits cannot sustain this throughput, set `parallel: false` to revert to sequential execution.

**GitHub API retries**: Critical GitHub API calls (posting reviews, comments) retry on 502, 503, 429, and ETIMEDOUT with fixed backoff.

**Truncation recovery**: When an LLM response is truncated (hit max tokens), the action attempts to salvage valid findings from the partial JSON rather than discarding the entire agent output.

## Token usage

After each review run, a collapsible **Token usage by agent** table is appended to the **review body** — the same comment that carries the findings (Approved / Changes Requested / Comment). The long-lived PR summary comment carries only the first-run walkthrough and is not rewritten on subsequent runs.

The table layout adapts based on cache activity:

| Column | Description | When shown |
|--------|-------------|------------|
| Agent | Agent name | Always |
| Model | Human-readable model name (e.g. "Sonnet 4.6") | Always |
| Input | Input tokens consumed | Always |
| Output | Output tokens generated; shown as `actual / cap` when a per-agent output cap is configured | Always |
| Cache Write | Tokens written to prompt cache | When any row has cache activity |
| Cache Read | Tokens read from prompt cache | When any row has cache activity |
| Total | Combined token count | Always |
| Est. Cost | Estimated cost at public list prices | Always |

When `LLM_PROMPT_CACHING` is active (default `auto` for Anthropic/Bedrock), the table expands to 8 columns showing Cache Write and Cache Read alongside the standard columns.

The `judge-pass` row appears as a regular agent row (included in the Total) when the judge actually ran:

| Row | Description | When shown |
|-----|-------------|------------|
| `judge-pass` | Tokens consumed by the judge-pass LLM call; included in Total | When `AI_JUDGE_PASS=true` (default) and the judge ran on a non-empty finding set |

Two supplementary rows may appear after the **Total** row. They are informational only and do not affect cost totals:

| Row | Description | When shown |
|-----|-------------|------------|
| Context enrichment | Token count of the `<symbol-context>` block prepended to agent prompts | When `AI_CONTEXT_ENRICHMENT=1` and the enrichment block was non-empty |
| Language profiles | Maximum profile tokens injected across all agents (per-agent routing, v2.1.0+) | When language profiles were injected and the count was non-zero |
| SARIF ingestion | Wall-clock elapsed time for parsing SARIF files (e.g. `0.34s`) | When `AI_SARIF_PATHS` is configured |

Costs are calculated using public list prices and do not reflect enterprise discounts, committed use agreements, or proxy markups. The table is also written to the [GitHub Actions step summary](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#adding-a-job-summary) for easy access from the Actions run page.
