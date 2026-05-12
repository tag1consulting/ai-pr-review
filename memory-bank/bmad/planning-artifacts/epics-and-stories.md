---
title: "Epics & Stories: ai-pr-review Python Rework"
project: ai-pr-review
owner: Greg Chaix (Tag1 Consulting)
date: 2026-05-11
status: accepted
source_prd: memory-bank/bmad/planning-artifacts/prd-ai-pr-review.md
source_arch: memory-bank/bmad/planning-artifacts/architecture-ai-pr-review.md
source_adr: memory-bank/bmad/adr/0001-learning-store.md
---

# Epics & Stories — ai-pr-review Python Rework

## Index

| Epic | Title | Stories | Milestone | Parent issue |
|---|---|---|---|---|
| 0 | Golden Parity Harness | 6 | [Epic 0](https://github.com/tag1consulting/ai-pr-review/milestone/1) | [#194](https://github.com/tag1consulting/ai-pr-review/issues/194) |
| 1 | Python Core (Compute) | 10 | [Epic 1](https://github.com/tag1consulting/ai-pr-review/milestone/2) | [#195](https://github.com/tag1consulting/ai-pr-review/issues/195) |
| 2 | LLM, VCS, Dispatch, Posting | 13 | [Epic 2](https://github.com/tag1consulting/ai-pr-review/milestone/3) | [#196](https://github.com/tag1consulting/ai-pr-review/issues/196) |
| 3 | Opt-in Capabilities | 12 | [Epic 3](https://github.com/tag1consulting/ai-pr-review/milestone/4) | [#197](https://github.com/tag1consulting/ai-pr-review/issues/197) |
| 4 | Soak, Observability, Default Flip | 10 | [Epic 4](https://github.com/tag1consulting/ai-pr-review/milestone/5) | [#198](https://github.com/tag1consulting/ai-pr-review/issues/198) |
| 5 | Delete Bash | 8 | [Epic 5](https://github.com/tag1consulting/ai-pr-review/milestone/6) | [#199](https://github.com/tag1consulting/ai-pr-review/issues/199) |

**59 stories total across 6 epics + 6 epic issues = 65 GitHub issues.** One PR per epic.

Story format below: `#NNN — short description — primary PRD requirement`.

## Epic 0 — Golden Parity Harness

**Parent**: #194 — **Milestone**: [Epic 0](https://github.com/tag1consulting/ai-pr-review/milestone/1) — **Branch**: `rework/epic-0-harness`

- #200 — Record mode for LLM and VCS API calls — `0.FR-1`
- #201 — Seed fixture corpus (10+ real-PR fixtures) — `0.FR-2`
- #202 — Payload-level diff harness (`tests/golden/diff_harness.py` + `tolerances.md`) — `0.FR-3`, `0.FR-7`
- #203 — Inline-eligibility oracle — `0.FR-4`
- #204 — Config parity matrix (authoritative env-var enumeration) — `0.FR-5`
- #205 — Parity CI workflow gating PRs — `0.FR-6`

## Epic 1 — Python Core (Compute Only)

**Parent**: #195 — **Milestone**: [Epic 1](https://github.com/tag1consulting/ai-pr-review/milestone/2) — **Branch**: `rework/epic-1-python-core` — **Closes on merge**: #126, #185, #187, #188, #190 (and #186 if S7a taken)

- #206 — Python package scaffold — `1.FR-1`
- #207 — Engine switch for compute — `1.FR-1`, `1.FR-9`
- #208 — Typed config (pydantic `ReviewConfig`) — `1.FR-2`
- #209 — Diff computation and line-mapping — `1.FR-3`
- #210 — Language detection and file manifest — `1.FR-4`
- #211 — Findings pipeline — `1.FR-5`, `1.FR-6` — closes #126, #185, #187, #190
- #212 — Analyzer bridge with pre-filter — `1.FR-7` — closes #188
- #213 — CVE lockfile scanning (scope call) — `1.FR-10` — #186 implement or defer
- #214 — Pricing port — `1.FR-8`
- #215 — Compute-to-posting JSON handoff — `1.FR-9`

## Epic 2 — LLM, VCS, Dispatch, Posting

**Parent**: #196 — **Milestone**: [Epic 2](https://github.com/tag1consulting/ai-pr-review/milestone/3) — **Branch**: `rework/epic-2-llm-vcs-dispatch` — **Closes on merge**: #100, #129, #181, #182, #183, #184, #191, #192

- #216 — LLM client port (5 providers) — `2.FR-1`
- #217 — Agent roster registry — `2.FR-2` — closes #129, #191
- #218 — Agent dispatch and parallelism — `2.FR-3`
- #219 — Conditional gates port — `2.FR-4`
- #220 — pr-summarizer sequence diagrams — `2.FR-5` — closes #100
- #221 — Outcome classifier (single source) — `2.FR-6` — closes #181, #192
- #222 — Watermark policy (per-agent) — `2.FR-7` — closes #182
- #223 — Ownership marker (marker-gated stale cleanup) — `2.FR-8`, `2.FR-10` — closes #183, #184
- #224 — VCS protocol + GitHub provider — `2.FR-9`, `2.FR-10`
- #225 — GitLab provider — `2.FR-9`
- #226 — Bitbucket provider — `2.FR-9`
- #227 — Remove compute handoff shim — `2.FR-11`
- #228 — Expand fixtures (failed-agent, competing-bot, incremental) — `2.AC-3`, `2.AC-4`

## Epic 3 — Opt-in Capabilities

**Parent**: #197 — **Milestone**: [Epic 3](https://github.com/tag1consulting/ai-pr-review/milestone/4) — **Branch**: `rework/epic-3-opt-in-capabilities` — **Closes on merge**: #24, #189

**Blocking dependency**: [ADR-0001](../../adr/0001-learning-store.md) ✅ accepted 2026-05-11 (Backend A: committed file on bot-managed branch).

### Capability A — Tree-sitter + ripgrep (opt-in via `AI_CONTEXT_ENRICHMENT=1`)

- #229 — Tree-sitter parsers (9 grammars) — `3.FR-A1`, `3.FR-A2`
- #230 — Symbol cross-file lookup (ripgrep) — `3.FR-A3`
- #231 — Context budget + injection — `3.FR-A4`, `3.FR-A5`, `3.FR-A6`
- #232 — Context parity carveout in harness — `3.FR-A7`

### Capability B — SARIF ingestion (opt-in via `sarif-paths` input)

- #233 — SARIF 2.1.0 ingestor — `3.FR-B1`, `3.FR-B2`, `3.FR-B3`, `3.FR-B4`
- #234 — SARIF example workflows — `3.FR-B5`

### Capability C — Slash commands + learning store (GitHub-only, per ADR-0001)

- #235 — Slash command parser and handlers — `3.FR-C1`, `3.FR-C2`, `3.FR-C7`
- #236 — Learning store (`GitBranchStore` per ADR-0001) — `3.FR-C3`, `3.FR-C4`
- #237 — Store retention (count + age + shared budget hook) — `3.FR-C5` — closes #24
- #238 — Prompt injection (`<repo-feedback>` gated by `AI_FEEDBACK_LOOP=1`) — `3.FR-C6`
- #239 — `/explain` and `/revise` agent callback — `3.FR-C8`
- #240 — Docs + first-dismissal privacy warning — `3.FR-C9`

## Epic 4 — Soak, Observability, Default Flip

**Parent**: #198 — **Milestone**: [Epic 4](https://github.com/tag1consulting/ai-pr-review/milestone/5) — **Branch**: `rework/epic-4-soak-and-flip` — **Closes on merge**: #153

- #241 — Structured logging — `4.FR-1`
- #242 — Telemetry hooks — `4.FR-2`
- #243 — Cost table refinement — `4.FR-3`
- #244 — Perf tuning + cache-priming investigation — `4.FR-4`, `4.NFR-1` — closes #153
- #245 — Error-surface polish (fail-soft standardization) — `4.FR-5`
- #246 — Docs refresh (Python primary) — `4.FR-6`
- #247 — Field soak (blocks flip) — `4.FR-7`, `4.NFR-2`
- #248 — Deprecation warning on `AI_PR_REVIEW_ENGINE=bash` — `4.FR-8`
- #249 — Flip default engine to `python` (gated on #247) — `4.FR-9`
- #250 — Release announcing Python-as-default — `4.FR-10`

## Epic 5 — Delete Bash

**Parent**: #199 — **Milestone**: [Epic 5](https://github.com/tag1consulting/ai-pr-review/milestone/6) — **Branch**: `rework/epic-5-delete-bash`

Execution-order note: #256 (analyzer wrapper audit) must complete before #251 so we know what (if anything) needs to be inlined.

- #256 — Analyzer wrapper audit — `5.FR-6`
- #251 — Delete orchestrator and libs — `5.FR-1`
- #252 — Delete post scripts — `5.FR-2`
- #253 — Delete bats suite — `5.FR-3`
- #254 — Entrypoint simplification — `5.FR-4`
- #255 — Container slim-down — `5.FR-5`, `5.NFR-1`
- #257 — Docs final cleanup — `5.FR-7`
- #258 — Final release

---

## Existing issues folded into this rework

All labeled `rework`; commented linking to the parent epic and resolving story. None closed yet — they close when their parent epic's PR merges.

| Issue | Epic | Resolving story | PRD ref |
|---|---|---|---|
| #24  | 3 (#197) | #237 | `3.FR-C5` |
| #100 | 2 (#196) | #220 | `2.FR-5` |
| #126 | 1 (#195) | #211 | `1.FR-5`, `1.FR-6` |
| #129 | 2 (#196) | #217 | `2.FR-2` |
| #153 | 4 (#198) | #244 | `4.FR-4` |
| #181 | 2 (#196) | #221 | `2.FR-6` |
| #182 | 2 (#196) | #222 | `2.FR-7` |
| #183 | 2 (#196) | #223 | `2.FR-8`, `2.FR-10` |
| #184 | 2 (#196) | #223 | `2.FR-8` |
| #185 | 1 (#195) | #211 | `1.FR-6` |
| #186 | 1 (#195) | #213 (scope call) | `1.FR-10` |
| #187 | 1 (#195) | #211 | `1.FR-6` |
| #188 | 1 (#195) | #210 + #212 | `1.FR-4`, `1.FR-7` |
| #189 | 3 (#197) | #229–#232 | `3.FR-A1`–`3.FR-A7` |
| #190 | 1 (#195) | #211 | `1.FR-5` |
| #191 | 2 (#196) | #217 | `2.FR-2` |
| #192 | 2 (#196) | #221 | `2.FR-6` |

## Progress tracking

- **Live view**: [Milestones page](https://github.com/tag1consulting/ai-pr-review/milestones) — auto-computed %-complete per epic.
- **Filter by initiative**: [issues labeled `rework`](https://github.com/tag1consulting/ai-pr-review/issues?q=is%3Aissue+label%3Arework).
- **Filter by epic**: each epic's milestone page lists its stories.
- **Per-epic PRs**: one PR per epic; PR body closes story issues via `Closes: #NNN` lines.





