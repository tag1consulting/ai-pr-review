---
title: "Architecture: ai-pr-review Python Rework"
project: ai-pr-review
owner: Greg Chaix (Tag1 Consulting)
date: 2026-05-11
status: draft
source_prd: memory-bank/bmad/planning-artifacts/prd-ai-pr-review.md
---

# Architecture — ai-pr-review Python Rework

## Contents

1. [Executive Summary](#executive-summary)
2. [Python Module Layout](#python-module-layout)
3. [Engine-Switch Strategy](#engine-switch-strategy)
4. [Payload-Level Golden Harness](#payload-level-golden-harness)
5. [Data Flow — End-to-End Review](#data-flow--end-to-end-review)
6. [LLM Client Architecture](#llm-client-architecture)
7. [VCS Provider Architecture](#vcs-provider-architecture)
8. [Agent Dispatch & Concurrency](#agent-dispatch--concurrency)
9. [Findings Pipeline](#findings-pipeline)
10. [Context Enrichment (Tree-sitter + ripgrep)](#context-enrichment-tree-sitter--ripgrep)
11. [SARIF Ingestion](#sarif-ingestion)
12. [Slash Commands & Learning Store](#slash-commands--learning-store)
13. [Observability](#observability)
14. [Testing Architecture](#testing-architecture)
15. [Container Image Layout](#container-image-layout)
16. [Failure Modes & Fail-Soft Rules](#failure-modes--fail-soft-rules)

## Executive Summary

A Python orchestrator replaces the bash orchestrator, living in the same container image. The CLI entry point `python -m ai_pr_review` auto-detects the host CI platform from environment variables and dispatches to the right VCS provider. LLM calls use official SDKs where available (`anthropic`, `openai`, `google-generativeai`) with `httpx` fallback; VCS calls use `PyGithub`, `python-gitlab`, and direct `httpx` for Bitbucket. Agents run concurrently via `asyncio.gather` with bounded concurrency. Every finding is a pydantic `Finding` model; every outbound VCS payload is a pydantic request model. Analyzer wrappers (`analyzers/run-*.sh`) stay as bash subprocesses — they are thin CLI adapters and porting them delivers no user value.

The design deliberately keeps bash around for two epics (parallel implementations gated by `AI_PR_REVIEW_ENGINE`), then flips the default after a soak, then deletes bash. The payload-level golden harness is the regression oracle across this transition.

## Python Module Layout

```
ai_pr_review/                    # Python package, entry point: python -m ai_pr_review
├── __init__.py
├── __main__.py                  # Delegates to cli.py
├── cli.py                       # Click-based CLI: `compute`, `review`, `slash-handle`
├── config.py                    # ReviewConfig (pydantic) — all env vars typed
├── errors.py                    # AI_PR_REVIEW_* exception hierarchy
├── logging.py                   # Structured JSON logger + correlation IDs (Epic 4)
├── telemetry.py                 # Optional metrics sink (Epic 4)
├── pricing.py                   # Model pricing, cost tables (Epic 1)
├── languages.py                 # detect_language(), is_test_file() (Epic 1)
├── manifest.py                  # build_file_manifest() + typed changed-file arrays (Epic 1)
│
├── diff/                        # Epic 1
│   ├── __init__.py
│   ├── compute.py               # Full + incremental diffs; SHA watermark
│   ├── linemap.py               # diff-hunk → provider-specific position/line/side
│   └── eligibility.py           # inline + suggestion-range eligibility (matches vcs/common.sh:98)
│
├── findings/                    # Epic 1
│   ├── __init__.py
│   ├── models.py                # pydantic Finding, ReviewOutcome
│   ├── extract.py               # Parse LLM/analyzer JSON → Finding
│   ├── merge.py                 # Dedup preserving distinct nearby findings (#185)
│   └── suppress.py              # Suppressions with httpx timeouts (#187)
│
├── llm/                         # Epic 2 (Epic 1 uses existing llm-call.sh)
│   ├── __init__.py
│   ├── client.py                # Router: picks provider from config
│   ├── anthropic.py             # Anthropic SDK + cache_control: ephemeral
│   ├── openai.py                # OpenAI SDK + shared-prefix cache layout
│   ├── google.py                # google-generativeai SDK + thinking-token accounting
│   ├── bedrock.py               # Bedrock proxy via httpx
│   ├── openai_compatible.py     # Azure / Groq / Together / local via httpx
│   └── tapes.py                 # Record/replay for golden fixtures
│
├── agents/                      # Epic 2
│   ├── __init__.py
│   ├── roster.py                # AgentSpec dataclass + AGENTS list (#129, #191)
│   ├── dispatch.py              # asyncio.gather fan-out, FAILED_AGENTS (#181)
│   ├── gates.py                 # detect_conditional_agent_triggers port (AI_DISABLE_GATE_*)
│   ├── summarizer.py            # pr-summarizer with typed Mermaid sequence diagrams (#100)
│   └── prompts.py               # Prompt assembly — loads prompts/*.md + addenda
│
├── review/                      # Epic 2
│   ├── __init__.py
│   ├── outcome.py               # classify_review_outcome() — single source (#181, #192)
│   └── watermark.py             # Per-agent watermark (#182)
│
├── vcs/                         # Epic 2
│   ├── __init__.py
│   ├── detect.py                # Auto-detect host CI from env
│   ├── base.py                  # VCSProvider protocol
│   ├── marker.py                # Ownership marker (#183, #184)
│   ├── github.py                # PyGithub + httpx
│   ├── gitlab.py                # python-gitlab
│   └── bitbucket.py             # httpx (no stable SDK)
│
├── analyzers/
│   ├── __init__.py
│   ├── bridge.py                # Subprocess dispatch to existing run-*.sh (Epic 1)
│   └── sarif.py                 # SARIF 2.1.0 ingestion (Epic 3)
│
├── context/                     # Epic 3 — Capability A
│   ├── __init__.py
│   ├── treesitter.py            # Extract symbol references from diff hunks
│   ├── symbols.py               # ripgrep-backed cross-file definition lookup
│   └── budget.py                # Deterministic truncation to token budget
│
├── slash/                       # Epic 3 — Capability C (GitHub-only)
│   ├── __init__.py
│   ├── parser.py                # Parse /ai-pr-review <cmd> <args>
│   └── handlers.py              # Dispatch: false-positive, wont-fix, explain, revise, feedback
│
└── feedback/                    # Epic 3 — Capability C
    ├── __init__.py
    ├── store.py                 # Learning store — backend resolved by ADR-0001
    ├── adapter.py               # Per-VCS adapter hook
    ├── retention.py             # Rolling N=500 + age-based trim
    └── inject.py                # <repo-feedback> prompt injection
```

**Principles**:
- Each subpackage has a single responsibility and a typed public API.
- Circular imports forbidden — `config` and `errors` have no internal dependencies.
- Every subprocess boundary (`analyzers/`, `llm/`, `vcs/`) isolates its failure mode.
- All I/O goes through `asyncio` (LLM + VCS) or `subprocess` (analyzers).

## Engine-Switch Strategy

**Epic 1 (compute-only)**:
```
review.sh
├── if AI_PR_REVIEW_ENGINE=python:
│   ├── exec python -m ai_pr_review compute  # writes AI_PR_REVIEW_COMPUTE_OUTPUT
│   └── then: source post-review.sh (or -gitlab/-bitbucket)  # reads the JSON
└── else (default bash):
    └── existing code path
```

**Epic 2 (full Python path)**:
```
review.sh
├── if AI_PR_REVIEW_ENGINE=python:
│   └── exec python -m ai_pr_review review   # single process, handles everything
└── else (default bash):
    └── existing code path
```

**Epic 4**: `review.sh` flips the default to `python`; bash path reached only when `AI_PR_REVIEW_ENGINE=bash` is explicit.

**Epic 5**: `review.sh` deleted. ENTRYPOINT in `Dockerfile` invokes `python -m ai_pr_review review` directly.

**Key invariant**: the CI wrapper (`action.yml`, `container-action/action.yml`, GitLab and Bitbucket examples) sets the same environment regardless of engine. Engine selection is purely internal.

## Payload-Level Golden Harness

### Why payload-level

Findings-JSON-only assertions miss the highest-cost regression class: inline-comment payload rejection (lines that don't exist in the diff; suggestion ranges falling on removed lines; thread-state transitions that hide or dismiss unrelated bot reviews). Payload-level assertion catches these.

### Record mode

- Env-gated by `AI_PR_REVIEW_RECORD_DIR=<path>`.
- A thin wrapper in `llm-call.sh` and every `gh api` / `curl` invocation in `post-review*.sh` writes the full request URL, sanitized headers, body, and response to `<path>/<fixture>/<sequence>.json`.
- Secrets are redacted at the recording layer using the existing secret-masking helpers. A CI lint scans recorded fixtures for known secret patterns before merge.
- Non-determinism (timestamps, opaque request IDs) is recorded but elided in diffs via `tolerances.md`.

### Replay harness

```
tests/golden/
├── diff_harness.py           # Core replay engine
├── inline_eligibility.py     # Dedicated oracle for inline comments
├── tolerances.md             # Documented diff-elision rules
├── config_matrix.md          # Authoritative env-var enumeration
├── test_config_parity.py     # pytest gate on matrix completeness
└── fixtures/
    ├── gh-docs-only/
    │   ├── env.json          # Input env vars (AI_*, GH_*, PR_NUMBER, etc.)
    │   ├── diff.patch        # Captured diff
    │   ├── llm-tapes/        # Recorded LLM requests + responses
    │   ├── vcs-tapes/        # Recorded VCS API requests + responses
    │   └── expected.json     # Golden output (findings + outbound payloads)
    ├── gh-incremental/
    ├── gl-mixed-language/
    ├── bb-large-diff/
    ├── gh-failed-agent/
    ├── gh-competing-bot/
    └── with-context/         # Epic 3 — context-enrichment enabled
```

### Assertion dimensions

For each fixture, the harness checks:
1. **Findings JSON** — schema-valid, same set of findings (per tolerance).
2. **Outbound HTTP request bodies** — every POST/PATCH/PUT to any VCS API matches the expected payload URL + body.
3. **Watermark transitions** — the summary-comment watermark field advances (or doesn't) as expected.
4. **Thread-resolution API calls** — stale-cleanup only touches ownership-marked threads; competing-bot fixture proves this.
5. **Inline-eligibility** — for every inline finding, `position`/`line`/`side` validate against the recorded diff, and suggestion-range endpoints fall on added-or-context lines.

### CI integration

`.github/workflows/parity.yml` runs the harness on every PR. Initially (Epic 0) asserts bash-vs-bash determinism; Epic 1+ adds Python-vs-bash. A CI failure blocks merge.

## Data Flow — End-to-End Review

```
┌─────────────────────────────────────────────────────────────┐
│ CI runner invokes the container image                       │
│   Env: AI_PROVIDER, ANTHROPIC_API_KEY, PR_NUMBER, BASE_REF, │
│        HEAD_SHA, GITHUB_TOKEN (or GitLab/Bitbucket equiv),  │
│        AI_* flags                                           │
└─────────────┬───────────────────────────────────────────────┘
              ▼
┌─────────────────────────────────────────────────────────────┐
│ review.sh (Epic 0-4) / python -m ai_pr_review (Epic 5)      │
│  ├── vcs/detect.py — detect host CI → VCSProvider           │
│  ├── config.py — parse all env vars → ReviewConfig          │
│  └── Route by AI_PR_REVIEW_ENGINE                           │
└─────────────┬───────────────────────────────────────────────┘
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Python compute phase (Epic 1+)                              │
│  ├── diff/compute.py — full or incremental via watermark    │
│  ├── diff/linemap.py — per-provider position map            │
│  ├── languages.py + manifest.py — typed changed-file arrays │
│  ├── agents/gates.py — which agents run for this diff       │
│  ├── analyzers/bridge.py — dispatch only eligible analyzers │
│  └── agents/dispatch.py — asyncio.gather(agents + analyzers)│
└─────────────┬───────────────────────────────────────────────┘
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Findings pipeline                                           │
│  ├── findings/extract.py — parse LLM/analyzer JSON          │
│  ├── findings/merge.py — dedup, preserve distinct nearby    │
│  ├── findings/suppress.py — apply global + local rules      │
│  │                           (with httpx-bounded verify)    │
│  └── review/outcome.py — classify_review_outcome()          │
└─────────────┬───────────────────────────────────────────────┘
              ▼
┌─────────────────────────────────────────────────────────────┐
│ Posting (Epic 2+)                                           │
│  ├── vcs/marker.py — wrap all outgoing text with marker     │
│  ├── vcs/<provider>.py.post_summary()                       │
│  ├── vcs/<provider>.py.post_findings() (inline + body)      │
│  ├── vcs/<provider>.py.resolve_stale_threads() (marker-gated│
│  │                                             , after post)│
│  └── review/watermark.py — advance per-agent watermark      │
└─────────────┬───────────────────────────────────────────────┘
              ▼
    Exit 0 (review posted) or non-zero (all finding agents failed)
```

Slash-command flow (Epic 3, GitHub-only): a separate entry path `python -m ai_pr_review slash-handle` invoked by `.github/workflows/slash-commands.yml` → `slash/parser.py` → `slash/handlers.py` → one of: dismiss + `feedback/store.py` write, or re-invoke `agents/dispatch.py` for `/explain` / `/revise`.

## LLM Client Architecture

```
llm/client.py
    call(model: str, system: str, user: str, max_tokens: int, ...) -> LLMResponse
      │
      ├── route by AI_PROVIDER:
      │     anthropic → llm/anthropic.py
      │     openai → llm/openai.py
      │     google → llm/google.py
      │     bedrock-proxy → llm/bedrock.py
      │     openai-compatible → llm/openai_compatible.py
      │
      ├── tape mode? (LLM_TAPE_DIR or AI_PR_REVIEW_RECORD_DIR set)
      │     → llm/tapes.py intercepts; reads or writes
      │
      └── return LLMResponse (pydantic): text, tokens (input/output/cache_read/cache_write),
                                         model_id, elapsed_ms
```

**Provider-specific notes**:

- **anthropic**: official `anthropic` SDK. `cache_control: ephemeral` markers on the shared-cache prefix. Token accounting from `usage.input_tokens`, `usage.output_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`.
- **openai**: official `openai` SDK. Shared-cache request layout puts the shared review context first in the system message for automatic prefix caching. Uses `max_completion_tokens` for chat models; `max_tokens` reserved for `openai-compatible`. Temperature omitted for reasoning-capable models (`o3`, `o4-mini`, `gpt-5`, `gpt-5.5`).
- **google**: `google-generativeai` SDK. Thinking tokens extracted from `usage_metadata.thought_token_count`, added to output count for cost accounting, logged on stderr.
- **bedrock-proxy**: `httpx` client with Anthropic-compatible request/response shape. Supports `cache_control: ephemeral`.
- **openai-compatible**: `httpx` client for Azure / Groq / Together / local. Legacy `max_tokens` field.

**Retry policy (shared via `llm/client.py` retry decorator)**:
- Status codes retried: 408, 429, 500, 502, 503, 504, Cloudflare 520–524.
- Transient network errors (connection refused, timeout, DNS failure) retried.
- Exponential backoff with jitter. Controlled by `LLM_RETRY_COUNT` (default 3).

**Shared-cache layout (preserved from current bash behavior)**:
All agents in a cohort share a common prefix: review context + file manifest + PR description + language profile. Per-agent divergence (system prompt, diff excerpt) starts after the shared prefix. This maximizes cache hit rate across the cohort.

## VCS Provider Architecture

```
vcs/base.py
    class VCSProvider(Protocol):
        async def fetch_pr_metadata(self) -> PRMetadata: ...
        async def fetch_diff(self, base_sha: str, head_sha: str) -> str: ...
        async def fetch_pr_description_and_commits(self) -> PRContext: ...
        async def post_summary(self, body: str, watermark: Watermark) -> SummaryRef: ...
        async def post_findings(self, findings: list[Finding], outcome: ReviewOutcome) -> PostResult: ...
        async def resolve_stale_threads(self, marker: str) -> CleanupResult: ...
        async def dismiss_stale_reviews(self, marker: str) -> CleanupResult: ...
        async def advance_watermark(self, sha: str, failed_agents: set[str]) -> None: ...
```

Concrete implementations:

- **`vcs/github.py`**: `PyGithub` for the REST surface; `httpx` for the Actions cache API and GraphQL `resolveReviewThread`. `post_findings` uses `createReview` with inline comments; marker embedded in the review body header. `resolve_stale_threads` runs *after* `post_summary` (fixes #183 ordering).
- **`vcs/gitlab.py`**: `python-gitlab` for REST; suggestion fences use GitLab's native `suggestion:-N+M` syntax. `resolve_stale_discussions` matches by both authenticated user *and* marker presence (fixes #184).
- **`vcs/bitbucket.py`**: `httpx` directly (no stable Python SDK). Single-summary-comment model. No inline, no suggestions, no approvals — matches current capability matrix.

**Marker semantics**:
- Every outgoing inline comment, review body, and PR-level summary comment from the Python engine starts with `<!-- ai-pr-review-inline -->` on its own line (or equivalent hidden HTML comment on GitLab).
- `resolve_stale_threads` / `dismiss_stale_reviews` require the marker *and* the authenticated bot user identity. Pre-Epic 2 bot comments without the marker are left untouched (documented migration note).

## Agent Dispatch & Concurrency

```
agents/roster.py
    AGENTS: list[AgentSpec] = [
        AgentSpec(
            name="pr-summarizer",
            prompt_path="prompts/pr-summarizer.md",
            tier=1,
            full_mode_only=False,       # runs first-run only
            conditional_trigger=FirstRunOnly(),
            max_output_tokens=4096,
            context_enrichment_eligible=False,
        ),
        AgentSpec(name="code-reviewer", tier=1, max_output_tokens=8192,
                  context_enrichment_eligible=True, ...),
        AgentSpec(name="silent-failure-hunter", tier=1,
                  conditional_trigger=HasErrorHandlingPatterns(),
                  context_enrichment_eligible=True, ...),
        AgentSpec(name="architecture-reviewer", tier=2, full_mode_only=True,
                  max_output_tokens=12288, context_enrichment_eligible=True, ...),
        AgentSpec(name="security-reviewer", tier=2, full_mode_only=True,
                  max_output_tokens=12288, context_enrichment_eligible=True, ...),
        AgentSpec(name="blind-hunter", tier=2, full_mode_only=True,
                  context_enrichment_eligible=False,   # diff-only by design (#189)
                  max_output_tokens=8192, ...),
        AgentSpec(name="edge-case-hunter", tier=2, full_mode_only=True, ...),
        AgentSpec(name="adversarial-general", tier=2, full_mode_only=True, ...),
    ]
```

**Concurrency**:
- Tier 1 agents + Tier 1-triggered analyzers: `asyncio.gather` with `asyncio.Semaphore(3)` for LLM calls. Analyzers run concurrently with no LLM-quota impact.
- Tier 2 (full mode): a second `asyncio.gather` with `Semaphore(5)`.
- Conditional gates (`gates.py`) filter the agent set before dispatch.
- Cache priming (opt-in `AI_CACHE_PRIMING=true`) serializes one cache-writing call before fan-out.

**Failure handling**:
- Each agent's call is wrapped in a try/except. Failures go into a typed `FailedAgent` record (name, reason, elapsed_ms).
- `agents/dispatch.py` returns `(successes: list[AgentResult], failures: list[FailedAgent])`.
- `review/outcome.py` consumes both and applies policy (#181): any failed finding agent → `may_approve=False`, `incomplete=True`.
- `review/watermark.py` advances per-agent; a global watermark advance requires all finding agents to succeed (#182).

## Findings Pipeline

```
LLM JSON / analyzer JSON
      │
      ▼
findings/extract.py
  - Validates against pydantic Finding model (#190)
  - Invalid findings: logged WARNING, dropped
  - Source tag normalized (agent name, analyzer name, or sarif:<tool>)
      │
      ▼
findings/merge.py
  - Group by (file, rule/check_id, normalized_text)
  - Collapse exact duplicates (same rule on same line)
  - Preserve distinct findings on nearby lines (#185)
  - Annotate survivors with union of source tags
      │
      ▼
findings/suppress.py
  - Load global config/suppressions.json
  - Load local <repo>/.github/ai-pr-review/suppressions.json if present
  - Apply match rules (file, line, code prefix, pattern regex)
  - For knowledge-cutoff verification types: httpx GET with connect_timeout=5,
    timeout=10, 1 retry (#187)
      │
      ▼
review/outcome.py
  - classify_review_outcome(findings, failed_agents, mode) -> ReviewOutcome
  - ReviewOutcome = {risk, event, may_approve, incomplete, finding_total}
  - Used identically by github.py, gitlab.py, bitbucket.py (#192)
```

**Finding model** (simplified):

```python
class Finding(BaseModel):
    severity: Literal["Critical", "High", "Medium", "Low"]
    file: str | None
    line: PositiveInt | None
    start_line: PositiveInt | None  # must be <= line if set
    finding: str  # human-readable text
    remediation: str | None
    confidence: conint(ge=0, le=100)
    source: str  # agent name / analyzer name / sarif:<tool> / "unknown"
    suggested_code: str | None  # no triple backticks (rejected at validation)
    rule_id: str | None
```

**Severity → review event mapping** (centralized in `review/outcome.py`):
- Critical/High → `REQUEST_CHANGES`
- Medium/Low → `COMMENT` informational (or `APPROVE` if all-success and no Critical/High)
- Any failed finding agent → never `APPROVE`, never GitLab-approve

## Context Enrichment (Tree-sitter + ripgrep)

**Gated by `AI_CONTEXT_ENRICHMENT=1`.** Off by default — zero behavioral change when unset.

### Flow

```
diff hunks (per file)
      │
      ▼
context/treesitter.py
  - Load grammar for detected language (fail-soft if missing: log WARNING, skip)
  - Parse the diff-hunk new-side content
  - Walk the AST, collect referenced symbols: function calls, type refs,
    imports, class instantiations, decorator targets
      │
      ▼
context/symbols.py
  - For each symbol, use ripgrep to find definitions across the repo checkout
  - ripgrep queries constrained by the file's language extensions
  - Return surrounding ±AI_CONTEXT_LOOKUP_LINES (default 8) per definition
  - Deduplicate: one definition per symbol, prefer same-file > same-package > repo-wide
      │
      ▼
context/budget.py
  - Budget: AI_CONTEXT_MAX_TOKENS (default 8192) per agent
  - Truncation order when over budget:
      1. Drop repo-wide definitions first
      2. Then drop same-package definitions
      3. Keep same-file definitions until exhausted
  - Serialize into <symbol-context>…</symbol-context> block
      │
      ▼
agents/prompts.py
  - Inject the block into the user message, only for agents with
    AgentSpec.context_enrichment_eligible=True
  - blind-hunter stays diff-only per #189 design
```

### Grammar bundle

9 tree-sitter grammars ship in the container image: Python, TypeScript, Go, PHP, Ruby, Rust, Shell, Java, C++. Target grammar pack size ≤50MB (NFR 3.NFR-1). Missing grammar for a detected language → log WARNING and continue without context (fail-soft).

### ripgrep integration

`ripgrep` is already in the image. `context/symbols.py` uses `subprocess.run` with bounded timeouts (per-query `timeout=3s`, per-file cap 50 queries). Cache lookups per review run (one symbol looked up once, even if referenced by multiple agents).

## SARIF Ingestion

**Gated by `AI_SARIF_PATHS` / `sarif-paths` action input.** Off by default.

### Flow

```
action.yml `sarif-paths: "codeql.sarif,trivy.sarif"`
      │
      ▼
analyzers/sarif.py
  - Parse each SARIF 2.1.0 file
  - For each result:
      severity = level_map(result.level):
        "error" → High, "warning" → Medium, "note" → Low
      file = result.locations[0].physicalLocation.artifactLocation.uri
      line = result.locations[0].physicalLocation.region.startLine
      finding = result.message.text
      rule_id = result.ruleId
      source = f"sarif:{runs[].tool.driver.name}"
      confidence = 90  # default; overridable via properties.confidence if present
  - Emit Finding instances
      │
      ▼
(flows into the standard findings pipeline:
 merge → suppress → outcome → post)
```

### Constraints

- **Additive**: bundled analyzers always run. SARIF is stacked on top.
- **Fail-soft**: unreadable SARIF file → log WARNING, continue without those findings.
- **Path hygiene**: SARIF file paths normalized to repo-relative before use.
- **Dedup**: SARIF findings go through the same dedup layer as native findings, so a duplicate from both CodeQL and the bundled semgrep collapses with union source tag (`sarif:CodeQL+semgrep`).

## Slash Commands & Learning Store

**GitHub-only in this rework.** GitLab/Bitbucket comment-event parity is Future Phase 7.

### Command surface

Parsed by `slash/parser.py`, dispatched by `slash/handlers.py`:

```
/ai-pr-review false-positive [reason]   (reply to inline finding)
/ai-pr-review wont-fix [reason]          (reply to inline finding)
/ai-pr-review explain                    (reply to inline finding)
/ai-pr-review revise <hint>              (reply to inline finding — hint required)
/ai-pr-review feedback <text>            (top-level PR comment — text required)
/ai-pr-review dismiss                    (alias for false-positive, no reason)
```

**Authorization**: `author_association` guard preserved — only OWNER/MEMBER/COLLABORATOR can trigger commands. Enforced by the consuming workflow's `if:` condition, identical to current behavior.

### Handler flow

```
GitHub issue_comment / pull_request_review_comment webhook
      │
      ▼
slash/parser.py
  - Regex extract: command, optional <arg>
  - Sanitize arg: length cap 1024, strip control chars
  - Validate: attached to inline finding if required; reject otherwise
      │
      ▼
slash/handlers.py
  ├── false-positive / wont-fix / dismiss:
  │     - Resolve the review thread via GraphQL resolveReviewThread
  │     - Compute FeedbackEntry: rule_id from finding, file_pattern, snippet,
  │       user, sha, event_type, created_at
  │     - feedback/store.py.append(entry)  [fail-soft]
  │
  ├── explain:
  │     - Re-invoke originating agent with single-finding context
  │     - Optionally enrich with tree-sitter context
  │     - Post reply on the thread
  │
  ├── revise:
  │     - Re-invoke originating agent with single-finding context + user hint
  │     - Agent either refines the suggestion or recants; reply on thread
  │
  └── feedback:
        - Top-level feedback; no specific finding
        - Store as repo-wide FeedbackEntry (file_pattern="*", rule_id=None)
```

### Learning store (per ADR-0001)

Storage model resolved by [ADR-0001](../../adr/0001-learning-store.md), accepted 2026-05-11: **committed `.ai-pr-review/learnings.jsonl` on a bot-managed branch** in the consumer's repo. Reason text is treated as public by default; users warned via bot reply on first dismissal.

**Fixed interface**:

```python
class FeedbackStore(Protocol):
    async def append(self, entry: FeedbackEntry) -> None: ...
    async def load_recent(self, limit: int = 500) -> list[FeedbackEntry]: ...
    async def trim(self, keep_count: int, max_age_days: int) -> int: ...
```

**Concrete implementation**: `GitBranchStore` (per-VCS adapters via PyGithub / python-gitlab / httpx). Branch default `ai-pr-review-bot`, configurable via `AI_FEEDBACK_BRANCH`. File path: `.ai-pr-review/learnings.jsonl`. Branch created lazily on first write (orphan branch with a single file). Concurrent writes: optimistic-lock retry on branch SHA, up to 3 attempts with jitter; after 3 failures, log WARNING and continue (fail-soft per `3.FR-C4`).

**FeedbackEntry schema** (pydantic):

```python
class FeedbackEntry(BaseModel):
    rule_id: str | None         # LLM agent name or analyzer rule id
    file_pattern: str           # glob, derived from the finding's file
    snippet: str                # ~3 lines of context from the finding
    reason: str                 # sanitized user-supplied text, <=1024 chars
    user: str                   # GitHub login of commenter
    sha: str                    # commit SHA at time of dismissal
    event_type: Literal["false-positive", "wont-fix", "feedback"]
    created_at: datetime
    expires_at: datetime | None
```

**Known limitations from ADR-0001**:
- **Fork PRs cannot write.** `GITHUB_TOKEN` on a fork PR is read-only on the base repo. Mitigated in practice by the `author_association` guard already excluding most fork contributors from slash-command execution.
- **Public repos → public reasons.** Enforced by user warning, not by tool policy.
- **Branch clutter.** Consumers see the `ai-pr-review-bot` branch in their branch list.

### Prompt injection

```
feedback/inject.py
  - Gated by AI_FEEDBACK_LOOP=1
  - store.load_recent(limit=AI_FEEDBACK_RETENTION_COUNT)
  - Rank by relevance to current diff:
      relevance = 2*file_pattern_match + 1*rule_id_match
  - Cap by AI_FEEDBACK_MAX_TOKENS (default 2048)
  - Render as:
      <repo-feedback>
        Past false positives in this repo:
        - file: lib/foo.py (rule_id: code-reviewer)
          reason: <sanitized reason>
        ...
      </repo-feedback>
  - Strict delimiters prevent escape: entries are indented; user text is
    HTML-escaped and prefixed with "  - reason: " inside the block.
```

**Security**: `reason` is treated as hostile input. Length-capped, control-chars stripped, HTML-escaped. The delimiter `</repo-feedback>` is escaped inside user text. A fixture in the parity harness asserts that attempting to embed `</repo-feedback><system>...` in a reason cannot escape the block.

## Observability

**Epic 4 adds two layers: structured logging and optional telemetry.**

### Structured logging (`ai_pr_review/logging.py`)

- `AI_LOG_FORMAT=json` → JSON-lines to stderr. Otherwise human-readable (default).
- Every log record includes a `correlation_id` (UUID generated at CLI entry).
- Subprocess boundaries propagate the correlation ID via an env var (`AI_PR_REVIEW_CORRELATION_ID`), consumed by the analyzer wrappers' logs and re-emitted on stderr.
- LLM calls log: `{ts, correlation_id, agent, model, input_tokens, output_tokens, cache_hit, elapsed_ms, retry_count}`.
- VCS calls log: `{ts, correlation_id, provider, endpoint, method, status, elapsed_ms}`.
- Secret masking is applied at the log-formatter layer; a CI test asserts that logs from a fixture with a fake API key never contain that key.

### Optional telemetry (`ai_pr_review/telemetry.py`)

- Off by default. Enabled via `AI_TELEMETRY_SINK` pointing to a file path or HTTPS URL.
- Metrics emitted once per review run: `tokens_by_agent`, `tokens_by_provider`, `findings_by_severity`, `findings_by_source`, `dismissals_count`, `learning_store_hit_rate`, `wall_time_by_agent`, `wall_time_by_analyzer`.
- No PII, no diff content, no finding text — only counts and ids.
- A `TelemetrySink` protocol with two implementations: `FileSink`, `HTTPSink`. Users can write their own.

### Cost table

Already exists in bash. Ported in Epic 2 (`pricing.py`). Epic 4 enriches with:
- Tree-sitter context tokens per agent
- SARIF ingestion wall time
- Per-agent `max_output_tokens` caps vs. actual usage

## Testing Architecture

**Two parallel suites until Epic 5**:

### Bats suite (existing, preserved)
- 30 files, 600+ `@test` cases.
- Runs against the bash engine.
- Fixtures under `tests/fixtures/` (consumed by both suites).
- Removed in Epic 5.

### Pytest suite (new)
- Lives in `tests/python/` with subdirectories mirroring `ai_pr_review/`.
- Fast unit tests: one test file per Python module.
- Integration tests: per-VCS provider, per-LLM provider, with tape-recorded fixtures.
- Property tests (hypothesis) for `diff/linemap.py` and `findings/merge.py`.
- `mypy --strict` and `ruff` as part of the pytest CI job.

### Golden harness (`tests/golden/`)
- Payload-level regression oracle.
- Runs both engines, diffs outputs, fails CI on out-of-tolerance diffs.
- Expanded per-epic with new fixtures (see per-epic PRD sections).

### Test-file detection

Preserved verbatim from `lib/languages.sh::is_test_file()` — ports to `ai_pr_review/languages.py::is_test_file()`.

| Pattern | Language |
|---|---|
| `*_test.go` | Go |
| `test_*.py`, `*_test.py` | Python |
| `*.test.[jt]sx?`, `*.spec.[jt]sx?` | JS/TS |
| `*_spec.rb`, `*_test.rb` | Ruby |
| `*Test.java` | Java |
| `*Test.php`, `*TestBase.php` | PHP |
| `*_test.cpp`, `*_test.cc`, `*_test.ts` | C++/TS |
| Any file under `/tests/`, `/test/`, or `/spec/` | Any |

## Container Image Layout

**Preserved (all epics)**:
- Multi-stage build.
- Multi-arch (linux/amd64, linux/arm64).
- Ubuntu 24.04 base.
- Non-root user (UID 1001).
- Analyzer binaries SHA-pinned at their current versions (shellcheck, trufflehog, golangci-lint, hadolint, kube-linter, tflint).
- Python dist-packages (ruff, semgrep, checkov) installed at current pinned versions.
- Composer vendor (phpcs, phpstan, Drupal coder) at current pinned versions.

**Epic 1+**:
- Python 3.12 added to final stage (already present for `checkov` and `semgrep`).
- `pip install` of the `ai_pr_review` package and its deps (`pydantic`, `httpx`, `anthropic`, `openai`, `google-generativeai`, `PyGithub`, `python-gitlab`, `click`) into a venv at `/opt/ai-pr-review`.
- `/opt/ai-pr-review/bin/ai-pr-review` on PATH as the Python CLI alias.

**Epic 3**:
- Tree-sitter grammar pack under `/opt/tree-sitter-grammars/` (≤50MB total).
- Ripgrep already present.

**Epic 5**:
- Remove `bats`, `bats-libs`, `jq` (if unused after analyzer-wrapper audit).
- Remove `bash`? No — analyzer wrappers still need it.
- Image size target: measurably smaller than the pre-Epic-5 baseline.

## Failure Modes & Fail-Soft Rules

| Failure | Behavior | Reasoning |
|---|---|---|
| LLM provider returns 429 | Retry with exponential backoff, up to `LLM_RETRY_COUNT` | Transient; retrying is cheaper than failing |
| LLM provider returns 500-series | Retry same policy | Transient |
| LLM provider times out mid-response | Salvage valid findings from partial JSON; mark agent as failed | Existing bash behavior preserved |
| All finding agents fail | Abort review with non-zero exit | Don't post a review based on zero signal |
| Some finding agents fail | Post review; `may_approve=False`; watermark for failed agents not advanced | #181, #182 |
| Analyzer binary missing | Log WARNING, treat as zero findings, continue | Existing behavior |
| Analyzer subprocess times out | Kill, log WARNING, continue | Bounded blast radius |
| SARIF file unreadable | Log WARNING, continue without those findings | Additive capability; don't block review |
| Tree-sitter grammar missing for detected language | Log WARNING, continue without context for that file | Fail-soft per 3.FR-A6 |
| Learning store unwritable (fork PR, permission denied) | Log WARNING, continue | 3.FR-C4 |
| `<repo-feedback>` injection exceeds token budget | Truncate by rank, log INFO | Deterministic |
| VCS API returns 403 on post | Fail the run with clear error (workflow config issue) | Not transient; user action required |
| GitHub stale-cleanup finds un-markered bot comment | Leave it alone, log INFO | #183, #184 |
| Watermark comment deleted between runs | Fall back to full-PR diff | Existing behavior |

**Never fail-soft**:
- Auth failures on post: exit non-zero, loud error.
- Malformed input from the CI runner (missing required env var): exit non-zero with actionable message.
- Secret detected in outgoing text that would be publicly visible: abort with error.



