---
title: 'Inject governance directives into LLM reviewer prompts'
type: 'feature'
created: '2026-05-26'
status: 'done'
context: []
baseline_commit: '00874fc53526300949f562515e6181a489e65a97'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Greg's `~/.claude/CLAUDE.md` and `~/.claude/agent-preamble.md` encode governance principles (Asimov's First Law, secret redaction, verify-before-naming, "don't reinvent the wheel") that guide his interactive Claude Code sessions but do **not** reach the LLM reviewers this tool dispatches against PR diffs. As a result, finding text and remediations from those reviewers can drift away from the same posture — flagging issues without a harm lens, missing reuse-of-existing-code findings, naming flags/functions from training recall, and potentially echoing real secret values back into PR comments.

**Approach:** Add a single shared prompt partial `prompts/_governance.md` that encodes the three principles that translate to a read-only diff reviewer (Asimov severity lens, DRY/reuse, verify-before-naming + secret redaction). Wire it into both engines' `effective_prompt()` composers (bash `lib/agents.sh`, Python `ai_pr_review/agents/dispatch.py`) ahead of the existing `_knowledge-cutoff.md` / `_trailer-findings.md` tail, so the seven finding-producing agents inherit the posture automatically. Always-on, no env-var toggle, mirroring the existing shared partials.

## Boundaries & Constraints

**Always:**
- Composition order is `base → _governance.md → _knowledge-cutoff.md → _trailer-findings.md → (suggestion-addendum.md)`. Governance goes at the front of the shared tail; the existing tail bytes stay byte-identical to preserve prompt-cache locality (see `lib/agents.sh:269-273` comment block).
- Bash and Python `effective_prompt()` implementations stay in lock-step, mirroring the existing `_knowledge-cutoff.md` / `_trailer-findings.md` patterns including the WARNING + base-prompt fallback semantics.
- The partial is ≤ ~50 lines, comparable density to `_knowledge-cutoff.md`. Token cost ships on every eligible-agent call.
- Applied to the same 7 finding-producing agents in `_AGENTS_WITH_FINDINGS_TRAILER` (code-reviewer, security-reviewer, architecture-reviewer, edge-case-hunter, blind-hunter, adversarial-general, silent-failure-hunter). pr-summarizer remains excluded.
- Must not break the existing `tests/python/agents/test_dispatch.py::test_effective_prompt_*` suite; new tests extend it.

**Ask First:**
- If `_governance.md` token cost in step-04 review pushes the partial materially beyond `_knowledge-cutoff.md` size, ask before merging — trim or accept.
- If live smoke-test (verify step) shows cache_read tokens vanish on Tier 2 agents, ask before merging — order may need re-evaluation.

**Never:**
- Do NOT add an `AI_GOVERNANCE_PREAMBLE` env var, `action.yml` input, or path-pluggable override file. Governance is foundational; making it optional invites silent disablement.
- Do NOT translate action-time CLAUDE.md rules (Checkpoint Triggers, force-push prevention, destructive git, subagent governance) into the partial. The reviewer LLMs are read-only; those rules don't apply and would just bloat prompts.
- Do NOT edit individual agent prompts (`code-reviewer.md` etc.). Per-agent customization is deferred unless the shared partial proves too generic in review.
- Do NOT add governance to `pr-summarizer.md`. Its output contract is summary text, not findings; the directives are framed for finding emission.
- Do NOT introduce US English rules or transparency-on-uncertainty rules into the partial — Greg deselected those and existing prompts already cover the intent via the confidence-floor (≥75) and knowledge-cutoff partial.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Finding-producing agent, all partials present | agent_name in `_AGENTS_WITH_FINDINGS_TRAILER`, all four shared files exist | Composed temp file contains: base prompt → _governance.md → _knowledge-cutoff.md → _trailer-findings.md, in that exact byte order | N/A |
| pr-summarizer dispatch | agent_name == "pr-summarizer" | base prompt path returned unchanged; no governance injected | N/A |
| `_governance.md` missing | finding-producing agent, file absent | Python: raises `FileNotFoundError` (mirrors `_knowledge-cutoff.md` and `_trailer-findings.md` behavior). Bash: emits WARNING to stderr and falls back to base prompt path | Required-fragment failure pattern, identical to existing partials |
| Suggestion-addendum eligible agent, all four shared files + addendum present | agent in `_AGENTS_WITH_SUGGESTION_ADDENDUM`, AI_ENABLE_SUGGESTIONS=true | Composed file: base → _governance → _knowledge-cutoff → _trailer-findings → suggestion-addendum | N/A |
| Cache-priming run on Anthropic provider | AI_CACHE_PRIMING=true, AI_PROVIDER=anthropic | Tier 2 agents still report cache_read tokens; existing tail bytes unchanged | If cache_read drops to 0, order broke prefix matching → ASK FIRST |

</frozen-after-approval>

## Code Map

- `prompts/_governance.md` — NEW. Three sections: Asimov First Law severity lens, Don't reinvent the wheel (DRY/reuse), Verify-before-naming + secret redaction. ~50 lines.
- `lib/agents.sh` — `effective_prompt()` at line 287; insert `_governance.md` ahead of `_knowledge-cutoff.md` in `parts` array. Extend the docstring comment block at lines 250-285 to mention the fourth shared block.
- `ai_pr_review/agents/dispatch.py` — `effective_prompt()` at line 121; add `governance_path`, FileNotFoundError raise mirroring cutoff/trailer pattern, prepend its `read_text()` to the parts list ahead of cutoff/trailer reads.
- `tests/python/agents/test_dispatch.py` — extend `effective_prompt*` test suite (currently lines 421-503) with four new test cases covering inclusion, ordering, missing-file raise, and updated finding-agent-trailers assertions.
- `tests/effective_prompt.bats` — NEW. Sources `lib/agents.sh` via the existing awk-extract harness pattern, asserts governance partial inclusion + ordering + WARNING-on-missing fallback for the bash side.

## Tasks & Acceptance

**Execution:**
- [x] `prompts/_governance.md` -- created with three sections (Asimov severity lens, DRY/reuse, verify-before-naming + secret redaction); 53 lines
- [x] `ai_pr_review/agents/dispatch.py` -- in `effective_prompt()`, added `governance_path` resolution + existence check + read_text() prepended to parts ahead of cutoff/trailer
- [x] `lib/agents.sh` -- in `effective_prompt()`, added `gv` local + existence check + WARNING+fallback + inserted into `parts` ahead of `kc`; extended docstring comment block to document the fourth shared block
- [x] `tests/python/agents/test_dispatch.py` -- added `test_effective_prompt_governance_order`, `test_effective_prompt_summarizer_gets_no_governance`, `test_effective_prompt_missing_governance_raises`; extended `_make_prompt_dir` and `test_effective_prompt_finding_agent_gets_trailers`; also updated the older `prompts.mkdir(exist_ok=True)` fixture at line 54 used by `run_tier` tests
- [x] `tests/call_agent.bats` -- extended existing `effective_prompt` suite (chose this over a new file since it already has comprehensive coverage); added `_install_shared_trailers` writing `_governance.md`; updated content/order assertions; renamed/added missing-fragment tests covering governance-missing, cutoff-missing, trailer-missing in the new check order

**Acceptance Criteria:**
- Given a finding-producing agent dispatched in either engine, when its effective prompt is composed, then the composed file's bytes match the exact order `base → _governance → _knowledge-cutoff → _trailer-findings → (suggestion-addendum)`.
- Given `_governance.md` is missing, when `effective_prompt()` runs in Python, then `FileNotFoundError` is raised; when it runs in bash, then a WARNING is emitted to stderr and the base prompt path is returned.
- Given the pr-summarizer agent, when dispatched, then the governance partial is NOT in its composed prompt.
- Given the existing test suite (`bats tests/*.bats` and `pytest tests/python/`), when the change is applied, then all pre-existing tests still pass and the four new Python tests + new bats file pass.
- Given a smoke-test PR review against a live test repo (`reference_live_test_repos.md`), when reviews run with the new partial, then bot findings show the new posture (at least one finding referencing existing-code reuse, OR seeded secret values redacted in remediation text), and Tier 2 cache_read tokens still appear when `AI_CACHE_PRIMING=true` on Anthropic provider.

## Verification

**Commands:**
- `pytest tests/python/agents/test_dispatch.py -k effective_prompt -v` -- expected: all green including 4 new test cases
- `bats tests/*.bats` -- expected: all 672 pre-existing tests pass + new `effective_prompt.bats` cases pass
- `shellcheck lib/agents.sh` -- expected: no new warnings vs. baseline
- `wc -l prompts/_governance.md` -- expected: ≤ ~60 lines (sanity check on size)

**Manual checks (if no CLI):**
- Read composed effective-prompt temp file from a Python test run; confirm visual order matches `base → _governance → _knowledge-cutoff → _trailer-findings`.
- Spot-check token cost delta: trigger one live review on the seeded test PR before vs. after; expect `TOKEN_LOG` input tokens up by roughly `(bytes of _governance.md / 4) × 7`. Materially larger means partial is too verbose.

## Suggested Review Order

**The actual content the LLMs will receive**

- The new partial — read this first; everything else is plumbing.
  [`_governance.md:1`](../../../prompts/_governance.md#L1)

**Python engine wiring**

- Resolve, validate-or-raise, prepend to parts ahead of cutoff/trailer.
  [`dispatch.py:152`](../../../ai_pr_review/agents/dispatch.py#L152)

**Bash engine wiring**

- Updated docstring documenting the fourth shared block + cache-locality rationale.
  [`agents.sh:254`](../../../lib/agents.sh#L254)

- New `gv` local + existence check + WARNING+fallback + insertion ahead of `kc`.
  [`agents.sh:336`](../../../lib/agents.sh#L336)

**Tests**

- Python composition-order assertion locks the cache-friendly layout.
  [`test_dispatch.py:438`](../../../tests/python/agents/test_dispatch.py#L438)

- Python missing-governance raise — mirrors cutoff/trailer pattern.
  [`test_dispatch.py:537`](../../../tests/python/agents/test_dispatch.py#L537)

- Python pr-summarizer-excluded check — confirms the early-return guard.
  [`test_dispatch.py:457`](../../../tests/python/agents/test_dispatch.py#L457)

- Bash composition-order assertion (parallel coverage to the Python test).
  [`call_agent.bats:299`](../../../tests/call_agent.bats#L299)

- Bash missing-governance fallback (warn + base prompt path).
  [`call_agent.bats:320`](../../../tests/call_agent.bats#L320)

- Test-fixture updates: `_install_shared_trailers` + `_make_prompt_dir` + the older `_make_context` fixture all write `_governance.md` so existing tests still pass.
  [`call_agent.bats:208`](../../../tests/call_agent.bats#L208)
  [`test_dispatch.py:54`](../../../tests/python/agents/test_dispatch.py#L54)
  [`test_dispatch.py:412`](../../../tests/python/agents/test_dispatch.py#L412)
