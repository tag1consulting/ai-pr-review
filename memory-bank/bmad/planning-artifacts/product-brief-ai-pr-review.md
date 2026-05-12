---
title: "Product Brief: ai-pr-review Python Rework"
project: ai-pr-review
owner: Greg Chaix (Tag1 Consulting)
date: 2026-05-11
status: draft
source_plan: /home/gchaix/.claude/plans/please-plan-a-major-elegant-pudding.md
---

# Product Brief — ai-pr-review Python Rework

## Executive Summary

`ai-pr-review` is a mature, VCS-agnostic pull-request review tool maintained by Tag1 Consulting. It runs LLM review agents and 13 bundled static analyzers against a PR/MR diff, then posts a structured review (summary comment + inline findings + suggestion fences) back to GitHub, GitLab, or Bitbucket. Today it is ~15,344 shell LOC with 600+ `bats` test cases across 30 files, 5 LLM providers, 3 VCS providers, and a hardened Docker image as its canonical distribution.

This brief covers a **major rework** of the tool: a full reimplementation of the orchestration layer in Python, inside the existing container image, while preserving every non-negotiable trait of the current product — VCS-agnosticism, action/wrapper input compatibility, findings JSON schema stability, and the bundled analyzer posture. The rework adds three capabilities that bash cannot practically deliver: tree-sitter + ripgrep symbol context enrichment, SARIF ingestion as an additive analyzer input, and (GitHub-only in this phase) an interactive slash-command surface with a learning loop that captures the *reason* users dismiss findings and injects those learnings into future agent prompts.

The rework will be executed as six epics (Epic 0–5), one PR per epic, over a multi-quarter horizon. A payload-level golden-fixture harness ships first (Epic 0) as the regression oracle; bash remains the default engine until a real field soak passes (Epic 4); only then is bash deleted (Epic 5).

## Problem Statement

Four problems justify the rework:

1. **Shell as an orchestration language has hit its limits for this codebase.** The orchestrator, LLM client, findings pipeline, and three VCS providers together total ~15,000 LOC of bash + jq. Critical behavior — diff-to-line mapping for inline comments, multi-provider API payload shaping, outcome classification, stale-thread cleanup — is spread across shell, `jq` filters, and provider-specific scripts. Recent production issues (#181–#184, #190, #192) show a pattern of bugs and refactor debt that are tractable in a typed language and painful in bash.

2. **Diff-to-line mapping produces silent regressions.** LLMs occasionally return line numbers that fail GitHub's inline-comment validation. Today, those findings are silently dropped to the body text. The existing tests catch findings JSON regressions but do not check the *payload* sent to the VCS API — the class of regression that matters most to users.

3. **Agents are context-starved.** Today's agents see only the diff plus a file manifest. They do not see imports, enclosing functions/classes, or referenced symbols. This produces predictable false positives ("X is undefined") and false negatives (missed security invariants in unchanged wrapper code). Issue #189 describes the need directly.

4. **Dismissed findings recur forever.** The `/ai-pr-review dismiss` command exists but captures no rationale and teaches the agents nothing. Users dismiss the same false positive on every push, on every PR, for the life of the repo.

The external architectural review that triggered this rework also recommended (a) decoupling analyzers entirely (SARIF-only ingestion) and (b) adding RAG/vector embeddings for repo-wide context. Both are deliberately out of scope for this rework: analyzers stay bundled (they're stable, SHA-pinned, and bundling is cheaper for consumers than forcing them to wire SAST pipelines), and RAG is deferred to a Future Phase in favor of lightweight tree-sitter + ripgrep context.

## Target Audience & Stakeholders

**Primary users**: engineering teams (at Tag1 Consulting and downstream consumers) who wire this action into their CI on GitHub, GitLab, or Bitbucket. They consume via `uses: tag1consulting/ai-pr-review/container-action@main`, a direct composite action reference, or a git submodule. They value:

- Zero-config onboarding (the quickstart in README is two steps — add a secret, drop a workflow file)
- Deterministic cost and latency (incremental reviews after first push; SHA watermark; token-cost table)
- Signal quality (suggestions with "Apply" buttons; stale-thread auto-cleanup; suppressions)
- Cross-VCS parity on summary/inline/suggestion coverage where the VCS supports it

**Secondary users**: Tag1 internal reviewers who open PRs against repos that use this tool, and who interact via the `/ai-pr-review *` slash commands on GitHub. They will be the first beneficiaries of the expanded slash command surface in Epic 3.

**Maintainers**: Greg Chaix (primary), Tag1 engineers contributing via PRs, and anyone downstream who pins the action. They value low cognitive overhead per change, a test suite that catches real regressions, and a codebase where a junior can trace a bug without reading 2,000-line shell scripts.

**Stakeholders without direct code access**: Tag1 finance/ops (token budgets, provider cost), security engineering (supply chain, container SBOM, secret handling), and compliance (OSS license audits on bundled analyzers).

## Product Vision

A pull-request review tool that:

- Continues to run as a VCS-agnostic container on every major CI platform (GitHub Actions, GitLab CI/CD, Bitbucket Pipelines)
- Catches real bugs and rejects junk findings, with a visibly shrinking false-positive rate as the tool learns from each repo it runs on
- Produces inline comments with suggestion fences that *actually land* on the right lines, every time
- Understands the code beyond the diff — imports, enclosing functions, referenced symbols — without the cost and operational weight of a vector store
- Lets a user dismiss a finding *with a reason* and trust that the next review will respect that reason
- Is readable, testable, and typed — so contributors (human or AI) can ship changes without fear

In one line: **VCS-agnostic, Python-powered, context-aware, learning-enabled code review.**

## Scope

### In scope for this rework (Epics 0–5)

**Engine migration**
- Full Python rewrite of the orchestrator, LLM client, findings pipeline, agent dispatch, and all 3 VCS providers, inside the existing container image.
- `AI_PR_REVIEW_ENGINE` env var selects bash or Python (default `bash` until Epic 4 field soak passes).
- Payload-level golden-fixture harness as regression oracle.

**Bug fixes folded into the port** (resolved as a side-effect of rewriting the relevant module):
- #126 `to_finding()` framework (Epic 1)
- #185 dedup dropping nearby findings (Epic 1)
- #187 suppression registry timeouts (Epic 1)
- #188 analyzer dispatch pre-filter (Epic 1)
- #190 finding schema validation (Epic 1)
- #100 pr-summarizer sequence diagrams (Epic 2)
- #129 declarative agent roster (Epic 2)
- #181 failed agents can produce approval (Epic 2)
- #182 watermark advances after partial failure (Epic 2)
- #183 GitHub stale cleanup hits other workflows (Epic 2)
- #184 GitLab stale cleanup hits unrelated bot discussions (Epic 2)
- #191 per-agent output token budgets (Epic 2)
- #192 centralized outcome classification (Epic 2)
- #153 cache-priming cost/benefit investigation (Epic 4)

**New capabilities** (opt-in only in Epic 3; each behind its own flag)
- **Tree-sitter + ripgrep symbol context** — injects referenced symbol definitions and enclosing functions into agent prompts. Resolves #189. Flag: `AI_CONTEXT_ENRICHMENT=1`.
- **SARIF ingestion** — consumer-supplied SARIF files flow through the same dedup/suppress/post pipeline as native analyzers. Additive, never replacing bundled tools. Flag: `AI_SARIF_PATHS` / `sarif-paths` action input.
- **Expanded slash commands + learning loop (GitHub-only)** — `/false-positive <reason>`, `/wont-fix <reason>`, `/explain`, `/revise <hint>`, `/feedback <text>`. User rationale persisted to a learning store resolved by ADR-0001. Resolves #24 directionally. Flag: `AI_FEEDBACK_LOOP=1` for prompt injection; store write is opt-in via ADR-resolved configuration.

### Explicitly out of scope (Future Phases)

- Vector DB / RAG for semantic repo-wide context (Phase 6)
- Cross-VCS slash-command parity — GitLab/Bitbucket comment-event parity (Phase 7)
- Fine-tuning / classifier over the learning store (Phase 8)
- Cross-repo organization-wide learning store (Phase 9)
- `@ai-pr-review <question>` conversational mode (Phase 10)
- Analyzer wrapper rewrite in Python (Phase 11)

## Non-Goals

- **No breaking changes to existing action/wrapper inputs.** A consumer who pins `@main` or `@v1.x.y` should not need to touch their workflow file to keep working reviews — only to opt into new capabilities.
- **No change to the findings JSON schema.** The schema is a stable contract with downstream tooling and must remain compatible.
- **No removal of bundled analyzers.** The 13 analyzer wrappers stay; SARIF is additive.
- **No first-class support for non-container consumers during the transition.** Direct-action and submodule paths continue to work against the bash engine until Epic 4 flip; Python is container-first.
- **No attempt to re-invent BATS in pytest.** Pytest suite parallels the bats suite via shared fixtures; bats stays until Epic 5.
- **No GitHub-exclusive features in the engine.** Every capability that is not GitHub-gated by VCS platform limits (slash commands) must work identically on GitLab and Bitbucket.

## Success Metrics

| Metric | Baseline (bash, May 2026) | Target (after Epic 4 flip) |
|---|---|---|
| Python-vs-bash end-to-end latency on parity fixtures | n/a | Within ±10% |
| Inline-comment API rejection rate | Measured in Epic 0 from production telemetry | Zero on golden-fixture replay; ≥50% reduction in production |
| False-positive recurrence rate on repos with feedback loop enabled | n/a | ≥50% reduction after 3+ dismissals of a given pattern |
| Findings-JSON schema conformance | ~not validated centrally | 100% (pydantic-enforced) |
| Test count | 600+ bats @test cases | 600+ bats + ≥500 pytest cases passing |
| Container image size | Baseline measured in Epic 0 | ≤+50MB after tree-sitter grammar additions; smaller post-Epic 5 |
| Time-to-onboard a new LLM provider | ~1 week (bash + curl + jq) | ≤2 days (typed SDK + unit tests) |
| Open maintenance-labeled issues | 18 (May 2026) | ≤5 after Epic 2; ≤2 after Epic 4 |

Qualitative success criteria:

- A new contributor can read one Python module and understand its contract without cross-referencing 3 other files.
- `/comprehensive-review` on a Python PR runs cleanly with zero critical findings.
- Downstream Tag1 repos (SIDashboard, lagoon-infra, tag1consulting.com) migrate to `AI_PR_REVIEW_ENGINE=python` voluntarily during the Epic 3 opt-in window and report positive signal.

## Constraints

**Non-negotiable (hard constraints):**

- **VCS-agnosticism.** The tool must continue to review PRs/MRs on GitHub Actions, GitLab CI/CD, and Bitbucket Pipelines from the same container image. No GitHub-specific architecture shortcuts. The Python CLI auto-detects the host CI platform from environment variables (`GITHUB_ACTIONS`, `GITLAB_CI`, `BITBUCKET_BUILD_NUMBER`).
- **Backward compatibility for existing consumers.** `action.yml` inputs stay identical in shape; env vars retain their current meaning; findings JSON schema is unchanged. Net-new inputs (`sarif-paths`, etc.) are additive and optional.
- **No user-supplied secrets leak into external text.** Placeholders only in any external-facing text (issues, PRs, commit messages, logs emitted to public workflow summaries). Enforced via existing secret-masking logic, preserved in the Python engine.
- **Never commit directly to `main`.** Every PR lands on a feature branch with a review, per standing workspace policy. One PR per epic.
- **Never retag a published release.** Fix-forward with patch releases only. Applies to any release cut during or after the rework.
- **Run `/comprehensive-review` before every release.** Applies to every epic PR and every subsequent release tag.

**Soft constraints:**

- Opt-in by default for every new capability. Consumers should never be forced into new behavior without explicitly flipping a flag.
- One PR per epic keeps review loads concentrated and merge overhead low, at the cost of larger diffs (acceptable tradeoff per approved plan).
- BMad artifacts (this brief, PRD, architecture, stories) stay local (gitignored under `memory-bank/`). GitHub issues are the durable record of user-visible work.
- Container image size growth capped at ~50MB for tree-sitter grammars (Epic 3).

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Parity fixtures drift during the rework because bash keeps shipping patches | Medium | High | Freeze non-critical bash changes during Epics 0–2; re-record fixtures on any behavioral bash change; document in `tests/golden/tolerances.md`. |
| LLM tape-playback diverges from live behavior (SDK differences, new API fields) | Medium | Medium | Keep tape-recorded fixtures small and canonical (10 seed fixtures); supplement with narrow unit tests per provider; periodic re-recording as SDKs evolve. |
| Posting-side bugs slip through findings-only assertions (as happens today) | High if not addressed | Critical | Epic 0 payload-level harness asserts on outbound POST bodies, thread-state transitions, watermark advances, and inline-eligibility per-finding. |
| `asyncio.gather` error propagation differs from bash `wait` and changes user-visible behavior | Medium | Medium | Targeted fixtures for race conditions and partial-failure scenarios; explicit typed `FAILED_AGENTS` set surfaced to posting. |
| Learning-store write permissions fail silently on fork PRs | High | Medium | ADR-0001 resolves this before Epic 3; fail-soft behavior: store-write failures log and continue, never block the review. |
| Prompt injection via user-supplied `<reason>` text | Medium | High | Strict delimiter-based injection with length caps; sanitize user-supplied text before prompt inclusion. |
| Tree-sitter grammars bloat the container image beyond acceptable limits | Medium | Low | Budget 50MB; measure in Epic 3; ship grammars as a slim set (9 languages) rather than the full tree-sitter ecosystem. |
| Consumers pinned to `AI_PR_REVIEW_ENGINE=bash` break on Epic 5 deletion | High | Low | Loud deprecation warning in Epic 4 with sunset date; Epic 5 only lands after the sunset window. |
| Soak duration too short → post-flip production bugs | Medium | High | Exit criterion: zero open P0/P1 Python-engine bugs for the final 7 days of the soak window. Gated as `story-4.7`. |
| `/comprehensive-review` cadence creates toil that slows epic PR velocity | Low | Low | Standing policy; absorb the cost. |

## Adoption Path

**Phase A — Opt-in early adopters (Epic 1+)**
Tag1 internal repos set `AI_PR_REVIEW_ENGINE=python` in their own workflow files. Engine runs in parallel to bash (still the default for external consumers). Bugs filed as GitHub issues tagged `engine:python`.

**Phase B — Opt-in new capabilities (Epic 3)**
Early adopters add `AI_CONTEXT_ENRICHMENT=1`, supply `sarif-paths`, and start using `/ai-pr-review false-positive <reason>`. ADR-0001 storage model is live. Feedback loop begins accruing per-repo learnings.

**Phase C — Soak (Epic 4)**
At least two downstream Tag1 repos run Python-engine opt-in for the duration of the soak window. Python-engine bugs fixed forward until the exit criterion is met. No default flip yet.

**Phase D — Default flip (end of Epic 4)**
`AI_PR_REVIEW_ENGINE=python` becomes the default. Bash remains selectable with a loud deprecation warning and a published sunset date. External consumers see no workflow change unless they set the env var explicitly.

**Phase E — Bash deletion (Epic 5)**
After the sunset window elapses, bash orchestration code is deleted in a single PR. Consumers who were pinned to bash see an unrecognized-env-var message with migration guidance.

## Open Questions

1. **ADR-0001 learning-store location** — blocking Epic 3 sprint planning. Three candidate storage models (committed bot-managed branch, workflow artifact + archive, external service); tradeoffs on permissions, privacy, concurrent writes, fork-PR writability.
2. **Golden-fixture LLM response sourcing** (Epic 0, `story-0.2`) — live-recorded canonical fixtures vs. synthesized. Recommended mix: ≤10 live, rest synthesized.
3. **Soak duration** (Epic 4, `story-4.7`) — calendar window vs. N PR cycles vs. pure zero-bug-criterion.
4. **CVE lockfile-aware scanning (#186) scope** — implement in Epic 1 `story-1.7` or defer. Depends on whether `osv-scanner` bundles cleanly into the image.
5. **Ownership-marker migration** (Epic 2, `story-2.8`) — how to handle pre-marker inline comments on existing PRs: treat as unowned, one-time sweep, or manual reset.

## Sources

- **Primary**: approved plan at `/home/gchaix/.claude/plans/please-plan-a-major-elegant-pudding.md` (the regenerated, critically-reviewed structure with Epics 0–5)
- **Secondary**: `CLAUDE.md`, `README.md`, `docs/agents.md`, `docs/features.md`, `docs/slash-commands.md`, `docs/static-analyzers.md`
- **Issue tracker context**: GitHub issues #24, #100, #126, #129, #153, #181–#192 on `tag1consulting/ai-pr-review`
- **External architectural review**: summarized in the plan's `## Where the External Reviewer's Feedback Was Mischaracterized` section

## Next Artifacts

This brief feeds directly into:

1. **PRD** (`memory-bank/bmad/planning-artifacts/prd-ai-pr-review.md`) — translates the scope and success metrics into explicit requirements per epic
2. **Architecture doc** (`memory-bank/bmad/planning-artifacts/architecture-ai-pr-review.md`) — Python module layout, engine-switch strategy, payload-harness design
3. **ADR-0001** (`memory-bank/bmad/adr/0001-learning-store.md`) — resolves Open Question 1 before Epic 3
4. **Epics & stories** (`memory-bank/bmad/planning-artifacts/epics-and-stories.md`) — per-epic story breakdown with acceptance criteria, driving GitHub issue creation


