---
title: "PRD: ai-pr-review Python Rework"
project: ai-pr-review
owner: Greg Chaix (Tag1 Consulting)
date: 2026-05-11
status: draft
source_brief: memory-bank/bmad/planning-artifacts/product-brief-ai-pr-review.md
source_plan: /home/gchaix/.claude/plans/please-plan-a-major-elegant-pudding.md
---

# Product Requirements Document — ai-pr-review Python Rework

## Contents

1. [Scope & Product Goals](#scope--product-goals)
2. [Epic 0 — Golden Parity Harness](#epic-0--golden-parity-harness)
3. [Epic 1 — Python Core (Compute Only)](#epic-1--python-core-compute-only)
4. [Epic 2 — LLM, VCS, Dispatch, Posting](#epic-2--llm-vcs-dispatch-posting)
5. [Epic 3 — Opt-in Capabilities](#epic-3--opt-in-capabilities)
6. [Epic 4 — Soak, Observability, Default Flip](#epic-4--soak-observability-default-flip)
7. [Epic 5 — Delete Bash](#epic-5--delete-bash)
8. [Cross-Epic Non-Functional Requirements](#cross-epic-non-functional-requirements)
9. [Backward-Compatibility Contract](#backward-compatibility-contract)
10. [Config Surface Tracking](#config-surface-tracking)

## Scope & Product Goals

This PRD translates the [product brief](product-brief-ai-pr-review.md) into explicit requirements per epic. Each epic lists:

- **User stories** (as a `<role>` I want `<capability>` so that `<outcome>`)
- **Functional requirements** (FR-N, testable)
- **Non-functional requirements** (NFR-N, testable)
- **Acceptance criteria** (AC-N, binary pass/fail)
- **Out-of-scope for this epic** (to prevent scope creep)

Requirements are numbered `<EPIC>.<CATEGORY>-<N>` (e.g., `1.FR-4` = Epic 1, Functional Requirement 4). A final section tracks cross-epic concerns and the backward-compatibility contract.

**Product-level goals** (from the brief):
- Ship a Python engine with payload-level parity to bash.
- Add tree-sitter context enrichment, SARIF ingestion, and a GitHub-only learning loop — each opt-in.
- Fold 14 existing issues into the port naturally; don't ship separate bash-side fixes.
- Preserve VCS-agnosticism and action/wrapper input compatibility throughout.
- Flip the default engine only after a real field soak.

---

## Epic 0 — Golden Parity Harness

**Goal**: Payload-level regression oracle ready before any engine code ships.

### User Stories
- As a **maintainer**, I want to replay any recorded PR fixture against both engines and see a structured diff of their outbound behavior, so that I can prove parity before flipping defaults.
- As a **contributor**, I want a CI job that fails on payload-level regressions, so that merge gating catches inline-comment and outcome bugs that findings-JSON diffs miss.
- As a **release owner**, I want a config matrix listing every env var and whether it is fixture-covered, so that release notes reflect real coverage rather than wishful documentation.

### Functional Requirements
- **0.FR-1** A record mode (env-gated `AI_PR_REVIEW_RECORD_DIR`) captures every outbound LLM request/response and every VCS API call made during a real review run, with request URL, headers (secrets redacted), body, and elapsed time. When the env var is unset, behavior is unchanged.
- **0.FR-2** A fixture corpus under `tests/golden/fixtures/` contains ≥10 real-PR captures spanning: docs-only diff; mixed-language diff (Python + TS + shell); secret-finding (trufflehog); CVE finding (OSV); large diff near the `max-diff-lines` cap; incremental re-review with SHA advance; full-mode agents run; failed-agent scenario (LLM timeout mocked); suppression hit; stale-thread cleanup. At least one fixture per VCS provider (GitHub, GitLab, Bitbucket).
- **0.FR-3** A replay harness (`tests/golden/diff_harness.py`) executes a fixture against an engine, collects (a) findings JSON, (b) every outbound HTTP request URL and body, (c) watermark state transitions, (d) thread-resolution API calls, and produces a structured JSON diff against the recorded-expected output.
- **0.FR-4** An inline-eligibility oracle asserts, for every finding that today yields an inline comment, that (i) `position`/`line`/`side` are valid against the recorded diff, (ii) suggestion-range endpoints fall on added-or-context lines (matches `vcs/common.sh:98` semantics), and (iii) body-only fallback triggers only when expected.
- **0.FR-5** A config parity matrix (`tests/golden/config_matrix.md`) enumerates every documented env var from `docs/configuration.md` with columns `name | source | purpose | covered-by-fixture | python-parity-proven`. Every var is either fixture-covered or explicitly marked `manual-test-only` with rationale.
- **0.FR-6** A CI workflow (`.github/workflows/parity.yml`) runs the harness on every PR touching the repo. Initially asserts bash-vs-bash determinism (self-consistency).
- **0.FR-7** Documented tolerance rules (`tests/golden/tolerances.md`) cover timestamp fields, opaque request IDs, ordering within unordered lists, and whitespace in rendered markdown.

### Non-Functional Requirements
- **0.NFR-1** Recording must not leak secrets: API keys, tokens, and anything matching known secret patterns are redacted before fixtures are written to disk. Enforced by a linter in the CI workflow.
- **0.NFR-2** Harness runtime per fixture ≤30s on a standard GitHub runner (no live network calls during replay).
- **0.NFR-3** Fixtures do not contain any user PII or customer code beyond what's already public in the repos they were recorded against.

### Acceptance Criteria
- **0.AC-1** Running the harness bash-vs-bash yields zero payload diffs across all ≥10 fixtures (self-consistency).
- **0.AC-2** The harness asserts on findings JSON, outbound POST bodies, watermark transitions, and thread-resolution calls — all four dimensions per fixture.
- **0.AC-3** The inline-eligibility oracle is green on every fixture where the current bash engine posts inline comments.
- **0.AC-4** The config parity matrix is published and lists every env var from `docs/configuration.md`.
- **0.AC-5** The parity CI workflow runs on every PR and gates merges.
- **0.AC-6** No user-visible behavior change on the bash engine (verified by running production reviews against two real repos before and after the Epic 0 PR merges).

### Out of Scope for Epic 0
- Python engine code (any). Epic 0 is test infrastructure only.
- Rewriting or modifying analyzer wrappers.
- Changes to `action.yml` or `container-action/action.yml`.

---

## Epic 1 — Python Core (Compute Only)

**Goal**: Port compute (config, diff, findings, analyzer bridge) to Python. Posting remains bash.

### User Stories
- As a **maintainer**, I want the compute half of the pipeline in typed Python backed by pydantic models, so that refactors are safe and schema violations surface at the earliest layer.
- As a **consumer**, I want docs-only PRs to skip unnecessary analyzer subprocesses, so that my CI runs are fast and cheap.
- As a **contributor**, I want every finding validated against a canonical schema, so that malformed LLM or analyzer output can't break rendering downstream.

### Functional Requirements
- **1.FR-1** A Python package `ai_pr_review/` provides an entry point `python -m ai_pr_review compute` invoked from `review.sh` when `AI_PR_REVIEW_ENGINE=python`. Engine default is `bash`.
- **1.FR-2** `ai_pr_review/config.py` maps every env var from the Epic 0 config matrix into a typed `ReviewConfig` (pydantic model). Unknown `AI_*` vars raise a helpful error naming the typo candidate.
- **1.FR-3** `ai_pr_review/diff/` modules port diff computation, SHA-watermark tracking, per-VCS line-mapping (GitHub `position`, GitLab `line`+`side`, Bitbucket equivalents), and inline-eligibility rules matching `vcs/common.sh:98` (added-lines-only for inline, added+context for suggestion range).
- **1.FR-4** `ai_pr_review/languages.py` ports `lib/languages.sh`; `ai_pr_review/manifest.py` ports `build_file_manifest` from `lib/diff.sh` and produces typed changed-file arrays (`shell`, `python`, `go`, `php`, `terraform`, `dockerfile`, `iac`, `js_ts`, `manifest_lockfile`).
- **1.FR-5** `ai_pr_review/findings/models.py` defines the pydantic `Finding` model enforcing the full schema (severity ∈ {Critical, High, Medium, Low}; integer line numbers; validated `source`, `confidence`, optional `suggested_code` with no backticks; optional `start_line` ≤ `line`). Resolves #190.
- **1.FR-6** `ai_pr_review/findings/extract.py`, `merge.py`, `suppress.py` port `lib/findings.sh`. Dedup preserves distinct nearby findings (resolves #185); suppression verification uses `httpx` with bounded `connect_timeout=5s` and `timeout=10s` and a cap of 1 retry (resolves #187).
- **1.FR-7** `ai_pr_review/analyzers/bridge.py` invokes existing `analyzers/run-*.sh` wrappers as subprocesses and uses the typed changed-file arrays to skip analyzers with no eligible files (resolves #188). Normalizes analyzer output into `Finding` instances.
- **1.FR-8** `ai_pr_review/pricing.py` ports `lib/pricing.sh` with pydantic-typed model pricing and token-table rendering.
- **1.FR-9** Python writes a stable intermediate JSON to `AI_PR_REVIEW_COMPUTE_OUTPUT` (env-var-configured path). The existing bash post scripts consume that file and continue posting. Epic 2 removes this shim.
- **1.FR-10 (scope call)** CVE lockfile-aware scanning (#186) is either implemented in `story-1.7a` via bundled `osv-scanner`, or explicitly deferred to a Future Phase with documented rationale captured in the PRD delta for Epic 1.

### Non-Functional Requirements
- **1.NFR-1** Compute-path latency under Python + bash-post within ±10% of pure bash on the Epic 0 parity-fixture set.
- **1.NFR-2** Pytest suite (`tests/python/`) passes alongside the existing bats suite; both run in CI.
- **1.NFR-3** Zero regressions on the Epic 0 payload harness (Python-compute + bash-post matches bash end-to-end on every fixture, within documented tolerances).
- **1.NFR-4** `mypy --strict` passes on `ai_pr_review/` modules delivered in this epic.
- **1.NFR-5** `ruff check` and `ruff format --check` pass on all new Python code.

### Acceptance Criteria
- **1.AC-1** Epic 0 payload harness green for every fixture with `AI_PR_REVIEW_ENGINE=python`.
- **1.AC-2** Config parity matrix: every env var is either fixture-covered or explicitly marked `manual-test-only`.
- **1.AC-3** Typed `Finding` schema rejects malformed LLM/analyzer output with an actionable error message (test: inject a malformed fixture and assert the specific error).
- **1.AC-4** Docs-only fixture spawns zero analyzer subprocesses (verified via process-count instrumentation in the test harness).
- **1.AC-5** Bats suite unchanged, still green.
- **1.AC-6** Closes GitHub issues #126, #185, #187, #188, #190 with references to the landing PR. #186 is either closed (if implemented) or updated with the documented deferral and linked to a Future Phase.

### Out of Scope for Epic 1
- LLM client port (stays bash in `llm-call.sh`).
- VCS provider post scripts (stay bash).
- Agent dispatch and conditional gates (stay bash).
- Any new user-visible capability (tree-sitter, SARIF, slash commands). Epic 3 territory.

---

## Epic 2 — LLM, VCS, Dispatch, Posting

**Goal**: Port the rest. After Epic 2, Python engine is self-sufficient end-to-end.

### User Stories
- As a **consumer**, I want inline comments to land on the right lines every time, so that I get actionable review feedback instead of body-text soup.
- As a **consumer running multiple bots**, I want ai-pr-review to never resolve or dismiss other workflows' reviews, so that security bots and linters stay visible.
- As a **maintainer**, I want a single declarative roster listing every agent, its tier, conditional trigger, and output-token budget, so that adding or retuning an agent is one file edit instead of four.
- As a **release owner**, I want any failed finding agent to force a non-approving outcome and block watermark advance, so that transient failures can't silently hide coverage gaps.

### Functional Requirements
- **2.FR-1** `ai_pr_review/llm/{anthropic,openai,google,bedrock,openai_compatible}.py` port `llm-call.sh` provider logic using official SDKs where available, `httpx` otherwise. Preserves prompt caching (Anthropic explicit `cache_control: ephemeral`; OpenAI shared-prefix layout), token-accounting output format, retry semantics, and all `AI_TEMPERATURE` / `LLM_PROMPT_CACHING` / `LLM_RETRY_COUNT` controls from current docs.
- **2.FR-2** `ai_pr_review/agents/roster.py` defines a typed `AgentSpec` with fields `name`, `prompt_path`, `tier` (1 or 2), `conditional_trigger`, `max_output_tokens`, `full_mode_only`, `context_enrichment_eligible`. Replaces dual-path dispatch logic. Resolves #129 and #191.
- **2.FR-3** `ai_pr_review/agents/dispatch.py` runs Tier 1 and Tier 2 agents via `asyncio.gather` with bounded concurrency matching current defaults (Tier 1: up to 3; Tier 2: up to 5). Cache-priming and effective-prompt assembly ported. Failed agents tracked in a typed `FAILED_AGENTS` set surfaced to posting.
- **2.FR-4** `ai_pr_review/agents/gates.py` ports `detect_conditional_agent_triggers` from `lib/diff.sh`. Preserves every kill-switch env var (`AI_DISABLE_GATE_*`). Consumes the typed changed-file arrays from Epic 1.
- **2.FR-5** `ai_pr_review/agents/summarizer.py` ports `pr-summarizer` with Mermaid sequence-diagram generation rebuilt around typed function-call traces (resolves #100).
- **2.FR-6** `ai_pr_review/review/outcome.py` implements a single `classify_review_outcome(findings, failed_agents, mode) -> ReviewOutcome` helper. `ReviewOutcome` is typed with `risk`, `event`, `may_approve`, `incomplete`, `finding_total`. Policy: any failed finding agent → `may_approve=False`, `incomplete=True` (resolves #181 and #192). All three VCS providers consume this helper.
- **2.FR-7** `ai_pr_review/review/watermark.py` implements per-agent watermarks. Advances only for agents that succeeded. When a global advance is blocked, emits a body-text explanation naming the failed agents (resolves #182).
- **2.FR-8** `ai_pr_review/vcs/marker.py` produces the ownership marker (`<!-- ai-pr-review-inline -->`) that every inline comment and review body includes. Stale-cleanup paths require the marker (resolves #183 and #184).
- **2.FR-9** `ai_pr_review/vcs/base.py` defines a `VCSProvider` protocol: `fetch_pr_metadata`, `fetch_diff`, `post_summary`, `post_findings`, `resolve_stale_threads`, `dismiss_stale_reviews`, `advance_watermark`. Concrete implementations: `github.py` (uses `PyGithub` + `httpx`), `gitlab.py` (uses `python-gitlab`), `bitbucket.py` (uses `httpx` directly — no stable Python SDK).
- **2.FR-10** GitHub stale-cleanup runs *after* a successful post, not before (resolves the ordering concern in #183).
- **2.FR-11** Remove the `story-1.9` compute-handoff shim. Python engine runs as a single-process exec when `AI_PR_REVIEW_ENGINE=python`.

### Non-Functional Requirements
- **2.NFR-1** Parallel-dispatch wall-clock within ±20% of bash on Epic 0 fixtures.
- **2.NFR-2** Zero bash subprocess calls during a Python-engine run except the analyzer wrappers in `analyzers/run-*.sh`.
- **2.NFR-3** `mypy --strict` and `ruff check` clean on all new Python code.
- **2.NFR-4** Per-agent token budgets visible in the final cost-table output (`AgentSpec.max_output_tokens` rendered per row).

### Acceptance Criteria
- **2.AC-1** Epic 0 payload harness green on Python end-to-end for all three VCS providers.
- **2.AC-2** Five LLM providers pass unit tests against tape-recorded responses (at least one "happy path" fixture per provider, one "transient 429" retry fixture per provider).
- **2.AC-3** Failed-agent fixture proves no approval and no watermark advance.
- **2.AC-4** Competing-bot fixture proves marker-gated stale cleanup never touches un-markered comments from other workflows.
- **2.AC-5** Per-agent token-budget field rendered in cost logs.
- **2.AC-6** Bats suite still green on bash engine.
- **2.AC-7** Closes GitHub issues #100, #129, #181, #182, #183, #184, #191, #192.

### Out of Scope for Epic 2
- Tree-sitter context, SARIF ingestion, slash-command expansion, learning store. Epic 3.
- Default-engine flip. Epic 4.
- Deleting bash. Epic 5.

---

## Epic 3 — Opt-in Capabilities

**Goal**: Ship three Python-only capabilities behind opt-in flags. Default engine stays `bash`.

**Blocking dependency**: **ADR-0001 (learning-store)** must be written and approved before this epic's sprint planning.

### User Stories
- As a **consumer**, I want the LLM agents to see the definitions of symbols referenced by the diff, so that I stop getting "undefined X" false positives for symbols defined in files outside the PR.
- As a **consumer who already runs CodeQL or other SARIF-producing scanners**, I want to feed their output into ai-pr-review's dedup/suppress/post pipeline, so that I get one unified review comment instead of three.
- As a **reviewer on a PR**, I want to reply `/ai-pr-review false-positive <reason>` to an inline finding and have the bot remember that reason on future reviews of this repo, so that I don't dismiss the same false positive fifty times.
- As a **maintainer**, I want each new capability to have its own independent kill switch, so that if tree-sitter misbehaves I can disable it without losing SARIF or slash commands.

### Functional Requirements — Capability A (Tree-sitter + ripgrep context)
- **3.FR-A1** Tree-sitter grammars for Python, TypeScript, Go, PHP, Ruby, Rust, Shell, Java, C++ ship in the container image.
- **3.FR-A2** `ai_pr_review/context/treesitter.py` extracts symbol references from diff hunks (function calls, type references, imports, class instantiations).
- **3.FR-A3** `ai_pr_review/context/symbols.py` uses ripgrep (already in the image) to locate definitions for referenced symbols across the repo checkout and returns surrounding ±N lines (`AI_CONTEXT_LOOKUP_LINES`, default 8).
- **3.FR-A4** `ai_pr_review/context/budget.py` enforces a total token budget per agent (`AI_CONTEXT_MAX_TOKENS`, default 8192). Truncation order when over budget: same-file → same-package → repo-wide.
- **3.FR-A5** Context is injected as a `<symbol-context>…</symbol-context>` block in agent user messages, only for agents with `AgentSpec.context_enrichment_eligible=True`. `blind-hunter` stays diff-only by design.
- **3.FR-A6** The entire capability is gated by `AI_CONTEXT_ENRICHMENT=1`. When unset (default), no tree-sitter parsing or ripgrep calls occur.
- **3.FR-A7** The Epic 0 parity harness gains a `--disable-new-capabilities` flag so pre-Epic 3 fixtures remain stable. A separate `tests/golden/fixtures/with-context/` directory records enriched-output fixtures.

### Functional Requirements — Capability B (SARIF ingestion)
- **3.FR-B1** A new action input `sarif-paths` and env var `AI_SARIF_PATHS` accept a comma-separated list of SARIF 2.1.0 file paths.
- **3.FR-B2** `ai_pr_review/analyzers/sarif.py` parses SARIF 2.1.0 and converts each `result` to the typed `Finding` model from Epic 1. Severity maps from SARIF `level` (`error`→High, `warning`→Medium, `note`→Low); confidence defaults to 90 for SARIF findings.
- **3.FR-B3** SARIF findings flow through the same dedup, suppress, and post pipeline as native-analyzer findings. Source tag is `sarif:<runs[].tool.driver.name>`.
- **3.FR-B4** Bundled analyzers continue to run in parallel. SARIF is additive, never a replacement.
- **3.FR-B5** Example workflows demonstrate the feature for each VCS provider: `examples/workflows/sarif-codeql.yml` (GitHub), `examples/gitlab-ci/sarif-trivy.yml`, `examples/bitbucket/sarif-container-scan.yml`.

### Functional Requirements — Capability C (Slash commands + learning loop, GitHub-only)
- **3.FR-C1** `ai_pr_review/slash/parser.py` parses these commands (each an optional `<reason>` argument unless noted):
  - `/ai-pr-review false-positive <reason>` (reply to inline finding)
  - `/ai-pr-review wont-fix <reason>` (reply to inline finding)
  - `/ai-pr-review explain` (reply to inline finding — no reason arg)
  - `/ai-pr-review revise <hint>` (reply to inline finding — hint required)
  - `/ai-pr-review feedback <text>` (top-level PR comment — text required)
  - `/ai-pr-review dismiss` (alias for `false-positive` with empty reason; backward compat)
- **3.FR-C2** `ai_pr_review/slash/handlers.py` dispatches parsed commands. GitHub wiring via `issue_comment` and `pull_request_review_comment` events, preserving the existing `author_association` guard.
- **3.FR-C3** `ai_pr_review/feedback/store.py` implements the storage model resolved by ADR-0001. Schema: `{rule, file_pattern, snippet, reason, user, sha, event_type, created_at, expires_at}`.
- **3.FR-C4** Store write failures fail soft: log a WARNING and continue the review. They must never block the review from posting.
- **3.FR-C5** `ai_pr_review/feedback/retention.py` enforces rolling N=500 entries by default and age-based trimming. Configurable via `AI_FEEDBACK_RETENTION_COUNT` and `AI_FEEDBACK_RETENTION_AGE_DAYS`.
- **3.FR-C6** `ai_pr_review/feedback/inject.py` loads recent learnings at review time, ranks by relevance (file-pattern overlap + rule-match), and injects a `<repo-feedback>…</repo-feedback>` block into agent prompts. Gated by `AI_FEEDBACK_LOOP=1` and capped by `AI_FEEDBACK_MAX_TOKENS` (default 2048).
- **3.FR-C7** User-supplied `<reason>` text is sanitized (length cap 1024 chars; control chars stripped) and delimited inside the prompt so that prompt-injection attempts cannot escape the feedback block.
- **3.FR-C8** `/ai-pr-review explain` and `/revise` re-invoke the originating agent in single-finding context (using learning store + tree-sitter context if enabled) and post a reply on-thread.
- **3.FR-C9** All new env vars and flags documented in `docs/configuration.md` feature-flags table. GitLab and Bitbucket docs explicitly state that interactive slash commands are a Future Phase.

### Non-Functional Requirements
- **3.NFR-1** Tree-sitter grammar additions grow the container image by ≤50MB.
- **3.NFR-2** Enabling context enrichment adds ≤500ms to overall review latency on a typical 500-line PR.
- **3.NFR-3** Store write contention: two concurrent reviews on the same repo must not corrupt the store (resolved by ADR-0001 choice of storage model).
- **3.NFR-4** Learning-store reads (every review that has `AI_FEEDBACK_LOOP=1`) are idempotent and cached per-run.

### Acceptance Criteria
- **3.AC-1** Tree-sitter context (opt-in) reduces false positives on the curated #189 fixture set — documented before/after counts in the PR.
- **3.AC-2** A CodeQL-emitted SARIF file passed via `sarif-paths` produces findings indistinguishable (after normalization) from native-analyzer findings on the same input.
- **3.AC-3** All new slash commands work on GitHub with `author_association` guard intact. Adversarial fixture: a user-supplied `<reason>` containing `</repo-feedback><system>…` cannot escape the delimiter.
- **3.AC-4** After ≥3 dismissals of a given pattern on a repo, the next review with `AI_FEEDBACK_LOOP=1` includes that pattern in `<repo-feedback>` — verified by golden fixture.
- **3.AC-5** Store write failures logged and review continues — verified by a fixture that forces the store into an unwritable state.
- **3.AC-6** Default engine remains `bash` (no flip in this epic).
- **3.AC-7** Closes GitHub issues #24, #189.

### Out of Scope for Epic 3
- Default-engine flip.
- GitLab and Bitbucket slash commands.
- Vector embeddings / RAG.

---

## Epic 4 — Soak, Observability, Default Flip

**Goal**: Stabilize Python as an opt-in, then flip the default after a real field soak.

### User Stories
- As a **maintainer**, I want structured JSON logs with correlation IDs across every subprocess boundary, so that I can trace an LLM call or analyzer run through the whole pipeline.
- As a **release owner**, I want an explicit soak-exit criterion, so that the default flip doesn't happen based on wishful thinking.
- As a **consumer**, I want docs that present Python as primary after the flip, with bash still reachable via a clear, deprecated env var.

### Functional Requirements
- **4.FR-1** `ai_pr_review/logging.py` emits structured JSON logs when `AI_LOG_FORMAT=json`; human-readable otherwise. Correlation IDs propagate across LLM calls, analyzer subprocesses, and VCS API calls.
- **4.FR-2** `ai_pr_review/telemetry.py` optionally emits metrics to a local file or webhook: token usage per agent, per-agent latency, analyzer latency, finding counts, dismissal events, learning-store hit rate. Off by default.
- **4.FR-3** Cost-table renders include tree-sitter context tokens (if enabled), SARIF-ingestion wall time (if used), and per-agent `max_output_tokens` caps.
- **4.FR-4** Cache-priming cost/benefit investigation (#153) completed in `story-4.4`. Outcome is one of: (a) keep default on with documented cost-savings fixture, (b) flip default off with documented rationale in `docs/architecture.md`, (c) remove the feature entirely.
- **4.FR-5** Fail-soft polish: unwritable learning-store, unreadable SARIF file, missing tree-sitter grammar all log WARNING and continue. Tested by fault-injection fixtures.
- **4.FR-6** Docs refresh: `docs/index.md`, `docs/getting-started.md`, `docs/architecture.md` present Python as primary. Bash internals move to `docs/bash-legacy.md`. `README.md`, `CONTRIBUTING.md`, `CLAUDE.md` updated.
- **4.FR-7** A soak log (`memory-bank/bmad/soak-log.md`) tracks Python-engine runs on at least two downstream Tag1 repos with counts of P0/P1/P2/P3 bugs filed and resolved.
- **4.FR-8** On `AI_PR_REVIEW_ENGINE=bash`, the engine logs a loud deprecation warning including the sunset date and a link to the migration guide.
- **4.FR-9** `story-4.9` flips the default `AI_PR_REVIEW_ENGINE` to `python` in `review.sh`. Gated on the soak exit criterion being met.
- **4.FR-10** The release announcing Python-as-default publishes a sunset date for bash deletion. Release notes list every new capability and every closed issue.

### Non-Functional Requirements
- **4.NFR-1** Python engine end-to-end latency within ±10% of bash on Epic 0 parity fixtures.
- **4.NFR-2** **Soak exit criterion**: zero open P0/P1 Python-engine bugs for the final 7 days of the soak window.
- **4.NFR-3** Jekyll docs build is green after the refresh.
- **4.NFR-4** Post-flip: `AI_PR_REVIEW_ENGINE=python` runs remain within ±10% latency on parity fixtures.

### Acceptance Criteria
- **4.AC-1** Latency NFR met and documented (benchmark attached to PR).
- **4.AC-2** Soak log shows exit criterion met.
- **4.AC-3** `AI_PR_REVIEW_ENGINE` default flipped to `python` in `review.sh`.
- **4.AC-4** Deprecation warning lands in bash-engine runs, visible in workflow summaries.
- **4.AC-5** Release tagged and announced with sunset date.
- **4.AC-6** Closes GitHub issue #153.

### Out of Scope for Epic 4
- Deleting bash code. Epic 5.
- New capabilities beyond polish.

---

## Epic 5 — Delete Bash

**Goal**: Mechanical deletion of bash orchestration code after the sunset window elapses.

### User Stories
- As a **maintainer**, I want a single engine to reason about, so that contributors onboard faster and refactors don't need to touch two languages.
- As a **consumer**, I want the container image smaller, so that my CI cold-starts faster.

### Functional Requirements
- **5.FR-1** Delete `review.sh`, `llm-call.sh`, `lib/agents.sh`, `lib/diff.sh`, `lib/findings.sh`, `lib/languages.sh`, `lib/pricing.sh`, `vcs/common.sh`.
- **5.FR-2** Delete `post-review.sh`, `post-review-gitlab.sh`, `post-review-bitbucket.sh`.
- **5.FR-3** Delete all `tests/*.bats` files and `tests/test_helper.bash`. Keep `tests/fixtures/` (pytest still uses it).
- **5.FR-4** `action.yml`, `container-action/action.yml`, `Dockerfile` ENTRYPOINT now invoke Python directly. Remove the `AI_PR_REVIEW_ENGINE` env var entirely (redundant).
- **5.FR-5** Remove `bats`, `jq` (if no longer used by analyzer wrappers after audit), and any other bash-only dependencies from `Dockerfile`. Verify image size decrease.
- **5.FR-6** Audit `analyzers/run-*.sh` for any accidental `source lib/*.sh` references. Inline anything found.
- **5.FR-7** Delete `docs/bash-legacy.md` and all remaining bash references from user-facing docs. Update `CONTRIBUTING.md` to drop bash contributor setup.

### Non-Functional Requirements
- **5.NFR-1** Container image is measurably smaller than the pre-Epic-5 baseline (documented in the PR).
- **5.NFR-2** pytest is the sole test runner in CI.
- **5.NFR-3** All three CI installation examples (GitHub Action, GitLab CI, Bitbucket Pipelines) run green against recorded fixtures.

### Acceptance Criteria
- **5.AC-1** `rg "\.sh$|\.bats$" review.sh lib/ vcs/` returns empty (analyzer wrappers in `analyzers/` stay).
- **5.AC-2** Image-size delta documented and negative.
- **5.AC-3** Three VCS installation examples green.
- **5.AC-4** Consumers pinned to `@main` require zero workflow changes beyond the deprecation warning going away.
- **5.AC-5** Final release tagged and announced.

### Out of Scope for Epic 5
- Rewriting `analyzers/run-*.sh` wrappers in Python (Phase 11).
- Any new capability.

---

## Cross-Epic Non-Functional Requirements

These apply to every epic PR and are verified at merge gate.

- **X.NFR-1** `/comprehensive-review` runs cleanly against the epic PR before merge (per standing user policy).
- **X.NFR-2** One PR per epic. No partial-epic PRs except where the plan explicitly scopes a story outside its epic (none currently).
- **X.NFR-3** No direct commits to `main`. Feature branch required.
- **X.NFR-4** No retagging of published releases. Fix-forward with patch releases.
- **X.NFR-5** No claude/AI attribution in any external text (commit messages, PR/issue bodies, release notes).
- **X.NFR-6** VCS-agnosticism preserved: the test matrix for every PR runs against GitHub, GitLab, and Bitbucket fixtures where applicable.
- **X.NFR-7** Every new env var added in any epic is documented in `docs/configuration.md` in the same PR that introduces it.
- **X.NFR-8** Every new capability in Epic 3+ has an independent kill switch.
- **X.NFR-9** Secret-masking preserved: no API key, token, or credential ever lands in a log, fixture, or test output. Enforced by a pre-commit lint and a CI gate.

## Backward-Compatibility Contract

This contract binds Epics 1 through 5. Violations require an explicit decision and changelog call-out.

### Stable (no breaking changes allowed without major version bump)
- **All existing action inputs in `action.yml` and `container-action/action.yml`**: `provider`, `api-key`, `base-url`, `model-standard`, `model-premium`, `review-mode`, `review-target`, `max-diff-lines`, `pr-number`, `base-ref`, `head-sha`, `github-token`, `parallel`, `max-inline`, `max-tokens-per-agent`, `enable-suggestions`.
- **All documented env vars from `docs/configuration.md`**: behavior and semantics preserved. The Epic 0 config matrix is the authoritative enumeration.
- **Findings JSON schema**: fields, types, and semantic meaning unchanged. Extensions are allowed only via net-new optional fields.
- **Ownership semantics of posted comments**: ai-pr-review comments carry the `<!-- ai-pr-review-inline -->` marker (new in Epic 2) but the comment body rendering format (severity icons, source tags, suggestion fences) stays compatible with prior renders.
- **The public container image location** (`ghcr.io/tag1consulting/ai-pr-review:latest` and version tags).

### Additive (may be added freely)
- New action inputs (e.g., `sarif-paths`).
- New env vars (e.g., `AI_CONTEXT_ENRICHMENT`, `AI_FEEDBACK_LOOP`).
- New slash commands.
- New Finding schema fields (additive, backward-compatible).

### Behavioral fixes allowed (counted as compatibility-neutral since they restore intended behavior)
- #181 (no approval with failed agents), #182 (no watermark advance with failed agents), #183/#184 (marker-gated cleanup), #185 (dedup correctness), #187 (suppression timeouts), #190 (schema validation).
- Each fix is accompanied by a CHANGELOG entry describing the behavior change and the issue closed.

## Config Surface Tracking

The authoritative env-var enumeration lives in `tests/golden/config_matrix.md` (produced in Epic 0 `story-0.5`). This PRD does not duplicate that matrix.

**Every env var added in any epic MUST be appended to the matrix in the same PR** with:
- Name
- Source (epic/story that introduced it)
- Purpose
- Covered-by-fixture (yes/no + fixture path)
- Python-parity-proven (yes/no, or `python-only` for capabilities that bash never had)
- Default value
- Kill-switch? (yes/no)

Known new env vars introduced by this rework (not exhaustive; authoritative list in the matrix):
- **Epic 0**: `AI_PR_REVIEW_RECORD_DIR`
- **Epic 1**: `AI_PR_REVIEW_ENGINE` (default `bash`), `AI_PR_REVIEW_COMPUTE_OUTPUT`
- **Epic 3**: `AI_CONTEXT_ENRICHMENT`, `AI_CONTEXT_MAX_TOKENS`, `AI_CONTEXT_LOOKUP_LINES`, `AI_SARIF_PATHS`, `AI_FEEDBACK_LOOP`, `AI_FEEDBACK_MAX_TOKENS`, `AI_FEEDBACK_RETENTION_COUNT`, `AI_FEEDBACK_RETENTION_AGE_DAYS` (subject to ADR-0001)
- **Epic 4**: `AI_LOG_FORMAT`, telemetry-related vars TBD in `story-4.2`
- **Epic 5**: removes `AI_PR_REVIEW_ENGINE`

## Traceability to Existing Issues

Authoritative mapping lives in the plan at `## Existing-Issue Folding`. Reproduced here for PRD traceability:

| Issue | Epic | Requirement(s) |
|---|---|---|
| #24 | 3 | 3.FR-C5, 3.FR-C6 (shared budget hook) |
| #100 | 2 | 2.FR-5 |
| #126 | 1 | 1.FR-5, 1.FR-6 |
| #129 | 2 | 2.FR-2 |
| #153 | 4 | 4.FR-4 |
| #181 | 2 | 2.FR-6 |
| #182 | 2 | 2.FR-7 |
| #183 | 2 | 2.FR-8, 2.FR-10 |
| #184 | 2 | 2.FR-8 |
| #185 | 1 | 1.FR-6 |
| #186 | 1 or Future | 1.FR-10 (scope call) |
| #187 | 1 | 1.FR-6 |
| #188 | 1 | 1.FR-4, 1.FR-7 |
| #189 | 3 | 3.FR-A1–A7 |
| #190 | 1 | 1.FR-5 |
| #191 | 2 | 2.FR-2 |
| #192 | 2 | 2.FR-6 |






