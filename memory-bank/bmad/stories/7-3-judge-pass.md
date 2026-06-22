# Story 7.3: LLM Candidate-Finding Judge Pass

**Epic:** 7 — Review-Quality & Perf Research
**Story ID:** 7-3
**Story Key:** 7-3-judge-pass
**GitHub Issue:** #360 (remainder — LLM judge half; provenance-weighting half landed as Story 6.1 / PR #528)
**Parent Epic Issue:** #362
**Status:** done

---

## Story

As a **maintainer**,
I want a cheap-model judge pass after candidate-finding extraction that down-ranks weak single-source findings,
so that false positives are reduced without re-sending the full diff and without silently hiding any real finding.

---

## Acceptance Criteria

1. A new module `ai_pr_review/findings/judge.py` implements `async def judge_findings(kept, *, llm_call, model, ...) -> list[Finding]`.
2. The judge sends ONE compact LLM call with candidate findings serialized as a JSON array: `{id, severity, confidence, sources, corroborated, file, line, finding, remediation}`. No diff text is included in the call.
3. The judge returns verdicts: `keep` | `downrank` per finding id. **`drop` is NOT a valid verdict** — the judge never removes a finding. Rationale: "a false positive is better than a missed vulnerability" (project posture).
4. Verdict application (deterministic Python, no LLM):
   - `downrank`: lowers `confidence` (by a fixed `JUDGE_DOWNRANK_AMOUNT = 15`, capped floor at 0) and routes the finding to the review body (sets `out_of_diff = True` to suppress inline posting). Neither verdict touches `severity`.
   - `keep`: finding is unchanged.
   - **Corroborated findings (`finding.corroborated is True`) are exempt from `downrank`** — the judge may not override independent static-analyzer + LLM-agent corroboration.
5. The judge is **fail-soft**: on any LLM error, `asyncio.TimeoutError`, malformed JSON, empty `kept`, or missing verdict field, it returns `kept` unchanged and logs a `WARNING`. A failed judge never modifies any finding.
6. A new prompt file `prompts/finding-judge.md` contains the judge instructions and the `json` output contract. The prompt must specify that verdicts are limited to `keep` or `downrank`, and that corroborated findings should always receive `keep`.
7. New `AI_JUDGE_PASS` config toggle (default `True` — on by default per explicit user decision):
   - `enable_judge_pass: bool = True` on `ReviewConfig`
   - `AI_JUDGE_PASS` in `_KNOWN_AI_VARS`
   - `_bool("AI_JUDGE_PASS", True)` in `from_env`
   - Thread into `OrchestrationConfig` (via `runtime.py:311-320`, mirroring `enable_context_enrichment`)
8. Orchestrator wiring — new **Phase 2.75** in `run_review` (`orchestrate.py`):
   - Insert between end of Phase 2.5 (`rollup_repeated_findings`, line ~200) and Phase 3 (`classify_review_outcome`, line ~206)
   - Guard: `if cfg.enable_judge_pass:`
   - Call: `kept = await judge_findings(kept, llm_call=llm_call, model=cfg.model_standard, ...)`
   - Log: number of findings downranked (emit as `INFO` with `judge: N finding(s) downranked`)
9. Tests:
   - `tests/python/test_judge.py` (new): verdict application correctness, corroborated exemption, fail-soft on error, JUDGE_DOWNRANK_AMOUNT constant
   - `tests/python/test_orchestrate.py` extension: judge-on path (canned verdict asserts both findings reach `post_findings` with correct confidence), judge-off path (unjudged list unchanged)
10. `mypy ai_pr_review/`, `ruff check`, `pytest tests/python -q` all pass.

---

## Implementation Tasks

- [x] **Task 1 — `prompts/finding-judge.md`: Judge prompt** (AC: 6)
  - [x] Write the judge system prompt:
    - Role: "You are a finding-quality judge. You receive a list of candidate code-review findings and return a verdict for each."
    - Instructions: rate each finding as `keep` (evidence is clear and specific) or `downrank` (vague, speculative, or unsupported without more context). Never respond with `drop`.
    - Corroboration rule: if `corroborated: true`, always return `keep` regardless of other signals.
    - Analyzer findings (sources starting with known analyzer prefixes) should lean toward `keep`.
    - Single-source adversarial-style findings (blind-hunter, adversarial-general) with vague text warrant `downrank`.
  - [x] Output contract section in the prompt:
    ```json
    {"verdicts": [{"id": <int>, "verdict": "keep"|"downrank", "reason": "<one line>"}]}
    ```
  - [x] Include an empty-state instruction: if all findings should be kept, return all with `keep`.

- [x] **Task 2 — `ai_pr_review/findings/judge.py`: Core module** (AC: 1-5)
  - [x] `JUDGE_DOWNRANK_AMOUNT: int = 15`
  - [x] `JudgeVerdict = Literal["keep", "downrank"]`
  - [x] `async def judge_findings(kept: list[Finding], *, llm_call: LLMCall, model: str, prompt_path: Path) -> list[Finding]`
    - Return `kept` immediately (no LLM call) if `not kept`
    - Build `user_message`: JSON array of `{id, severity, confidence, sources, corroborated, file, line, finding, remediation}` where `id` is the list index
    - Read `system_prompt` from `prompt_path`
    - Construct `LLMRequest(model_id=model, system_prompt=system_prompt, user_message=user_message, max_tokens=1024, temperature=0.0)`
    - Call `response = await llm_call(request)` inside a `try/except Exception`; on any error, log warning and return `kept` unchanged (fail-soft)
    - Parse `response.text` as JSON; extract `verdicts` list; on `json.JSONDecodeError` or missing key, log warning and return `kept` unchanged (fail-soft)
    - Apply verdicts deterministically (see below); return modified list
  - [x] `def _apply_verdicts(kept: list[Finding], verdicts: list[dict[str, object]]) -> tuple[list[Finding], int]`
    - Build id→verdict map; default missing ids to `keep`
    - For each finding by index:
      - `corroborated is True` → always `keep` (log DEBUG "judge: corroborated finding kept regardless of verdict")
      - `downrank` → `finding.model_copy(update={"confidence": max(0, finding.confidence - JUDGE_DOWNRANK_AMOUNT), "out_of_diff": True})`
      - `keep` → unchanged
    - Return `(modified_list, downrank_count)`
  - [x] Add `from __future__ import annotations`; full type annotations; module docstring

- [x] **Task 3 — `ai_pr_review/config.py`: Config toggle** (AC: 7)
  - [x] Add `AI_JUDGE_PASS` to `_KNOWN_AI_VARS` frozenset
  - [x] Add `enable_judge_pass: bool = True` to `ReviewConfig` (near `enable_feedback_loop`)
  - [x] Add `enable_judge_pass=_bool("AI_JUDGE_PASS", True)` in `from_env`

- [x] **Task 4 — `ai_pr_review/review/runtime.py`: Thread into `OrchestrationConfig`** (AC: 7, 8)
  - [x] Add `enable_judge_pass: bool` to `OrchestrationConfig` (or equivalent — check how `enable_context_enrichment` is threaded at lines 311-320 and mirror exactly)
  - [x] Set it from `cfg.enable_judge_pass`
  - [x] Thread `prompt_path` for the judge prompt (`script_dir / "prompts" / "finding-judge.md"`) if `OrchestrationConfig` carries prompt paths; otherwise pass it in the orchestrate call

- [x] **Task 5 — `ai_pr_review/orchestrate.py`: Phase 2.75** (AC: 8)
  - [x] Add import: `from ai_pr_review.findings.judge import judge_findings`
  - [x] After the `rollup_repeated_findings` block (line ~200, end of Phase 2.5) and before `classify_review_outcome` (line ~206):
    ```python
    # Phase 2.75: judge pass — down-rank weak single-source findings.
    if cfg.enable_judge_pass and kept:
        from ai_pr_review.findings.judge import judge_findings
        kept = await judge_findings(
            kept,
            llm_call=llm_call,
            model=cfg.model_standard,
            prompt_path=...,  # script_dir / "prompts" / "finding-judge.md"
        )
    ```
  - [x] Log the downrank count at INFO level after the call

- [x] **Task 6 — Tests** (AC: 9, 10)
  - [x] **New `tests/python/test_judge.py`:**
    - `test_judge_keep_unchanged` — verdict `keep` → finding identical
    - `test_judge_downrank_lowers_confidence` — `downrank` → `confidence -= JUDGE_DOWNRANK_AMOUNT`; `out_of_diff = True`
    - `test_judge_downrank_capped_at_zero` — confidence 5 + downrank → confidence 0, not negative
    - `test_judge_corroborated_exempt` — `corroborated=True` with verdict `downrank` → finding unchanged
    - `test_judge_missing_verdict_defaults_to_keep` — finding not in verdict map → unchanged
    - `test_judge_fail_soft_on_error` — `llm_call` raises `RuntimeError` → returns `kept` unchanged, logs warning
    - `test_judge_fail_soft_on_bad_json` — `llm_call` returns garbled text → returns `kept` unchanged
    - `test_judge_empty_input_no_llm_call` — empty `kept` → returns `[]` without calling `llm_call`
  - [x] **Extend `tests/python/test_orchestrate.py`:**
    - `test_orchestrate_judge_on_downranks_weak_finding` — `enable_judge_pass=True`; seed two findings (one weak single-source, one corroborated); canned LLM factory returns `downrank` for weak, `keep` for corroborated; assert both reach `post_findings`; weak has lower confidence + `out_of_diff=True`; corroborated is unchanged
    - `test_orchestrate_judge_off_leaves_findings_unchanged` — `enable_judge_pass=False`; assert findings reach `post_findings` with original confidence

- [x] **Task 7 — Verification** (AC: 10)
  - [x] `pytest tests/python -q` — full suite green
  - [x] `mypy ai_pr_review/` — clean (83+ source files)
  - [x] `ruff check ai_pr_review/ tests/python/` — clean
  - [x] `python -c "import ai_pr_review.findings.judge; import ai_pr_review.orchestrate"` — no import error

---

## Dev Notes

### Architecture Context

**Findings pipeline phase ordering** (from `orchestrate.py`):

```
Phase 1.5  inject cfg.extra_findings (SARIF + native analyzers)
Phase 2    extract per-agent → raw_findings → merge_findings → apply_suppressions → kept
Phase 2.5  apply_diff_scope + rollup_repeated_findings
Phase 2.75 judge_findings (NEW — this story)    ← insert here
Phase 3    classify_review_outcome  (keys off SEVERITY only, not confidence)
Phase 4    post summary → post findings
```

The judge operates on `kept: list[Finding]` — the final diff-scoped, rolled-up candidate list. At this point `llm_call` is already in scope (it is a `run_review` parameter at `orchestrate.py:94`). Nothing is posted yet. Inserting before Phase 3 ensures a downranked finding does not drive a CHANGES_REQUESTED outcome through reduced confidence (confidence does not affect outcome; severity does — and we do NOT touch severity).

### Why `out_of_diff = True` for Downrank

Setting `out_of_diff = True` routes a downranked finding to the review body (the summary comment) rather than as an inline PR comment. This is the correct observable effect: a weak finding the judge skeptical of should still be visible (we never hide it), but it should not interrupt the reviewer's inline flow. The `out_of_diff` flag is already used by the diff-scope phase for analyzer findings from outside the changed lines — reusing it for "judge-skeptical" routing is consistent.

### Cheap Model

Use `cfg.model_standard` (the non-premium model). Pattern verified: `_run_summarizer` in `cli.py:176` and `_run_issue_linker` in `cli.py:199` both hard-wire `rc.model_standard`. The judge prompt should be compact enough for the cheap model to score reliably (no reasoning required — just classify each finding).

### Corroborated Flag (AC: 4 exemption)

`Finding.corroborated` is already on `main` (merged PR #528, `models.py:39`). The judge module imports `Finding` from `ai_pr_review.findings.models` and reads `.corroborated` directly. No Finding model changes are needed.

### Import Cycle Check

`orchestrate → findings.judge → findings.models + llm.base + (prompt file read)`. `findings.judge` imports nothing from orchestrate. No cycle.

### Test Harness Pattern

`_llm_call_factory` in `test_orchestrate.py:116-136` matches on `req.system_prompt` marker substring to return canned output. For the judge-pass test, match on a phrase unique to `finding-judge.md` (e.g. `"finding-quality judge"`) and return canned verdict JSON. `_FakeProvider.findings_calls[0]["findings"]` is the list that reached `post_findings`.

### On-By-Default Caveat (carry into PR description)

`AI_JUDGE_PASS` defaults to `True` per the user's explicit decision (session 2026-06-22). This adds one cheap-model LLM call per review. The PR description and CHANGELOG must note this so consumers pinning `@main` or `@v2` are not surprised. Include a migration note: set `AI_JUDGE_PASS: false` in the action inputs to restore the pre-Epic-7 behavior. Consider a minor-version bump.

### What NOT to Do

- **Do not implement `drop` as a verdict** — the judge is down-rank-only.
- **Do not touch `severity`** — severity drives APPROVE/REQUEST_CHANGES/COMMENT outcome; changing it would silently flip PR review outcomes.
- **Do not let the judge add new findings** — only `keep`/`downrank` is valid; the judge receives the candidate list and returns a same-length-or-same list.
- **Do not block on judge failure** — always fail-soft: a bad judge response returns `kept` unchanged.

---

## Dev Agent Record

### Agent Model Used

_to be filled in_

### Debug Log References

_none_

### Completion Notes List

_to be filled in_

### File List

- `prompts/finding-judge.md` (new)
- `ai_pr_review/findings/judge.py` (new)
- `ai_pr_review/config.py` (modified)
- `ai_pr_review/review/runtime.py` (modified)
- `ai_pr_review/orchestrate.py` (modified)
- `tests/python/test_judge.py` (new)
- `tests/python/test_orchestrate.py` (modified)
