# Story 6.1: Provenance Weighting — Boost Confidence on Analyzer+Agent Agreement

**Epic:** 6 — Python-Engine Hardening
**Story ID:** 6-1
**Story Key:** 6-1-provenance-weighting
**GitHub Issue:** #360 (partial — the cheap deterministic sub-task; the LLM judge pass stays in Epic 7)
**Parent Epic Issue:** #361
**Status:** review

---

## Story

As a **maintainer**,
I want the findings pipeline to reward independent corroboration,
so that findings flagged by both a static analyzer AND an LLM agent at the same file+line region receive a confidence boost and a `corroborated` marker, making them more visible and less likely to be mistaken for single-source noise.

---

## Acceptance Criteria

1. `Finding` has a new `corroborated: bool = False` field (Pydantic `BaseModel`, `models.py`).
2. When `merge._collapse_cluster` collapses a proximity cluster whose unioned `sources` list contains **at least one analyzer source AND at least one LLM-agent source**, it sets `corroborated=True` and boosts `confidence` by `PROVENANCE_BOOST=10`, capped at 100.
3. The boost is deterministic and requires no LLM calls (zero token impact).
4. `corroborated` is **not** serialized in `Finding.to_dict()` — it is internal-only, like `out_of_diff`. This keeps the parity suite green by construction.
5. Unit tests cover all cases listed in the test plan below: corroboration boosts, non-triggers (two agents, two analyzers, empty, unknown sources), `osv` counts as analyzer, case-insensitive prefix match, cap at 100, distant findings not corroborated.
6. `mypy --strict` (entire `ai_pr_review/` package), `ruff check`, and `pytest tests/python -q` all pass.

---

## Implementation Tasks

- [x] **Task 1 — `scope.py`: Promote `_is_analyzer` predicate** (AC: 2)
  - [x] Add public function `is_analyzer_source(source: str) -> bool` that returns `True` when a single source string starts with any prefix in `_ANALYZER_PREFIXES` (case-insensitive).
  - [x] Refactor private `_is_analyzer(finding)` to delegate: `return any(is_analyzer_source(s) for s in sources)`. Behavior unchanged.
  - [x] Run `pytest tests/python/test_scope.py -q` — must stay green.

- [x] **Task 2 — `models.py`: Add `corroborated` field** (AC: 1, 4)
  - [x] Add `corroborated: bool = False` after the `out_of_diff` field (currently line 34), with the doc comment from the plan.
  - [x] Confirm `to_dict()` (lines 51-71) is NOT modified — `corroborated` does not appear in the serialized output.

- [x] **Task 3 — New `ai_pr_review/findings/provenance.py`** (AC: 2, 3)
  - [x] Module docstring.
  - [x] `PROVENANCE_BOOST: int = 10`
  - [x] `is_corroborated(sources: list[str]) -> bool` — `has_analyzer = any(is_analyzer_source(s) for s in sources)`, `has_agent = any(s in AGENT_NAMES for s in sources)`, returns `has_analyzer and has_agent`.
  - [x] `boosted_confidence(confidence: int) -> int` — `return min(100, confidence + PROVENANCE_BOOST)`.
  - [x] Imports: `from ai_pr_review.findings.scope import is_analyzer_source` and `from ai_pr_review.agents.roster import AGENT_NAMES`.
  - [x] `from __future__ import annotations` at top; all functions annotated.

- [x] **Task 4 — `merge.py`: Apply boost in `_collapse_cluster`** (AC: 2)
  - [x] Add import: `from ai_pr_review.findings.provenance import boosted_confidence, is_corroborated`.
  - [x] Replace the `return best.model_copy(update={"sources": sorted(all_sources)})` at line 109 with the dict-update pattern.
  - [x] The `update: dict[str, object]` annotation is required — mypy infers `dict[str, list[str]]` from the first key and rejects the `bool`/`int` assignments without it.

- [x] **Task 5 — New `tests/python/test_provenance.py`** (AC: 5, 6)
  - [x] Use a local `_sources` helper (list literal), NOT `_make_finding` — these are pure string tests.
  - [x] `is_corroborated` positive cases: `["semgrep", "security-reviewer"]`, `["sarif:bandit", "security-reviewer"]`, `["SEMGREP", "code-reviewer"]` (case-insensitive), `["osv", "security-reviewer"]`.
  - [x] `is_corroborated` negative cases: `[]`, `["code-reviewer", "blind-hunter"]` (agents only), `["semgrep", "ruff"]` (analyzers only), `["osv"]` (single analyzer), `["osv", "dependency-check"]` (`dependency-check` NOT in `AGENT_NAMES`), `["unknown", "ruff"]`, `["unknown", "code-reviewer"]`.
  - [x] `boosted_confidence` cases: `80 → 90`, `95 → 100`, `100 → 100`.
  - [x] Parametrized guard: every prefix in `scope._ANALYZER_PREFIXES` paired with `"code-reviewer"` → True (catches future prefix additions that miss the `is_analyzer_source` helper).

- [x] **Task 6 — `tests/python/test_findings.py`: Integration tests via `merge_findings`** (AC: 5, 6)
  - [x] Reuse the existing `_make_finding(**kw)` factory at line 17-26.
  - [x] `test_merge_corroboration_boosts_confidence` — two findings same file, lines 10 and 12 (within `PROXIMITY_LINES=3`), one `source="semgrep"` confidence=90, one `source="security-reviewer"` confidence=90. After `merge_findings`: 1 result, `corroborated is True`, `confidence == 100`.
  - [x] `test_merge_two_agents_no_boost` — two findings same location, sources `"code-reviewer"` and `"blind-hunter"`. After merge: `corroborated is False`, `confidence == 80` (unchanged).
  - [x] `test_merge_two_analyzers_no_boost` — sources `"semgrep"` and `"ruff"`. After merge: `corroborated is False`.
  - [x] `test_merge_single_finding_no_boost` — single finding, `corroborated is False`, `confidence` unchanged.
  - [x] `test_merge_corroboration_cap` — sources `"shellcheck"` (confidence=95) and `"security-reviewer"` (confidence=95). After merge: `confidence == 100` (not 105).
  - [x] `test_merge_distant_not_corroborated` — analyzer at line 10, agent at line 50 (>3 apart). Two separate findings; both `corroborated is False`.
  - [x] `test_merge_default_source_not_corroborated` — source `"test"` (neither analyzer nor agent). `corroborated is False`.
  - [x] **Regression guard:** Confirmed `test_merge_dedup_same_file_nearby_lines` and proximity-chaining test pass with `corroborated is False` for two-agent clusters.

---

## Dev Notes

### Architecture Context

**Findings pipeline phase ordering** (from `orchestrate.py`):

```
Phase 1.5  inject cfg.extra_findings (analyzer + SARIF) into raw_findings
Phase 2    extract_findings per agent → raw_findings
           merged = merge_findings(raw_findings, confidence_threshold=...)   # line 177
           kept = apply_suppressions(merged, suppression_rules)               # line 178
Phase 2.5  apply_diff_scope + rollup_repeated_findings
Phase 3    classify_review_outcome  ← keys off SEVERITY only, not confidence
Phase 4    post summary → post findings
```

The corroboration boost happens inside `merge._collapse_cluster`, which runs inside `merge_findings` at Phase 2. At that moment both analyzer and LLM-agent findings are in the `raw_findings` list and their `sources` are being unioned — exactly the right place for cross-source agreement detection.

**Why confidence only, not severity:** Severity drives outcome classification (`REQUEST_CHANGES` vs `APPROVE`), ranking (`merge.py:_SEVERITY_ORDER`), and inline-vs-body routing (`vcs/_inline.py`). Escalating severity on corroboration would silently flip a Low-severity PR to `REQUEST_CHANGES`. The AC says "improves confidence **or ranking**" — the `corroborated` flag is the observable signal; the confidence nudge is secondary.

**Why the boost runs AFTER the confidence threshold filter:** `merge_findings` (merge.py:38) drops findings with `confidence < threshold` (default 75) *before* clustering. This means a corroborated pair each at confidence 70 would be dropped before agreement is detected. This is intentional: we do NOT want provenance to rescue findings the operator explicitly excluded via `AI_CONFIDENCE_THRESHOLD`. All default analyzer confidences (shellcheck 95, most 90, phpstan/kube-linter 85, checkov 80, SARIF 90) clear the 75 threshold alone, so in practice the boost affects already-surviving findings.

### Critical File Locations (verified at c738ed5)

| File | Purpose | Key lines |
|------|---------|-----------|
| `ai_pr_review/findings/models.py` | `Finding` Pydantic model | `confidence` (19), `sources` (27), `out_of_diff` (34), `to_dict` (51-71) |
| `ai_pr_review/findings/merge.py` | Merge + dedup | `confidence filter` (38), `PROXIMITY_LINES=3` (24), `_collapse_cluster` (98-109) |
| `ai_pr_review/findings/scope.py` | Analyzer predicate | `_ANALYZER_PREFIXES` (35-50), `_is_analyzer` (60-65) |
| `ai_pr_review/agents/roster.py` | Agent name registry | `AGENT_NAMES: frozenset[str]` (176) |
| `ai_pr_review/findings/provenance.py` | **NEW** — this story | (create) |
| `tests/python/test_findings.py` | Merge tests | `_make_finding` factory (17-26), merge tests from line 226 |
| `tests/python/test_scope.py` | Scope tests | `test_is_analyzer_recognises_all_sources` (260-278) |
| `tests/python/test_provenance.py` | **NEW** — this story | (create) |

### Predicate Logic (exact)

**Analyzer source** — a source string is an analyzer source iff:
```python
s.lower().startswith(p) for any p in _ANALYZER_PREFIXES
```
where `_ANALYZER_PREFIXES` (scope.py:35-50) = `checkov, eslint, golangci-lint, hadolint, kube-linter, osv, phpcs, phpstan, ruff, sarif:, semgrep, shellcheck, tflint, trufflehog`.

**LLM-agent source** — a source string is an LLM-agent source iff:
```python
s in AGENT_NAMES
```
where `AGENT_NAMES` (roster.py:176) = `{pr-summarizer, code-reviewer, silent-failure-hunter, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general, issue-linker}`.

**cve-check edge case:** `source="osv"` → analyzer (osv is in `_ANALYZER_PREFIXES`). `agent="dependency-check"` is on the `agent` field, not in `sources`. `dependency-check` is NOT in `AGENT_NAMES`. A lone cve-check finding is analyzer-only, not corroborated. A `sources=["osv", "security-reviewer"]` proximity merge correctly triggers corroboration.

### Import Cycle Check

`merge → provenance → {scope, roster}`. `roster` imports nothing from `findings`. `scope` imports `diff.linemap + models`. `models` imports only pydantic. No cycle.

### mypy Gotcha

In `_collapse_cluster`, the `update` dict must be annotated as `dict[str, object]`. Without it, mypy infers `dict[str, list[str]]` from the first key assignment and rejects the `bool` and `int` values added by the corroboration branch.

### No Config Knob

`PROVENANCE_BOOST = 10` is a named constant in `provenance.py`. No env var, no `_KNOWN_AI_VARS` entry, no `Config` field threading. YAGNI — a conservative nudge needs no operator tuning knob. If the team ever wants `AI_PROVENANCE_BOOST`, it requires: `ReviewConfig` field, validator, `_int()` call, `_KNOWN_AI_VARS` entry (or `ConfigError`), threading through `OrchestrationConfig → merge_findings → _collapse_cluster`, `action.yml` docs. That is a separate, deliberate decision.

### Test Style Rules (from existing codebase)

- `_make_finding(**kw)` factory: `Finding.model_validate({"severity":"High","confidence":80,"finding":"Test","source":"test","file":"foo.py","line":10} | kw)`.
- String-level predicate tests go in `test_provenance.py` (no `Finding` construction needed).
- Use `assert f.corroborated is True` (not `== True`).
- For proximity tests, `PROXIMITY_LINES = 3` (merge.py:24): two findings ≤3 lines apart cluster; >3 lines apart do not.
- `merge_findings` default `confidence_threshold=75` — all test findings must have `confidence >= 75` or they are filtered before the boost has a chance to run.

### What NOT to Do

- **Do not touch `to_dict()`** (models.py:51-71). `corroborated` is internal-only, like `out_of_diff`.
- **Do not touch `severity`** — severity drives outcome/ranking; the AC does not ask for severity escalation.
- **Do not add a pre-merge rescue pass** — that would duplicate the clustering logic and rescue sub-threshold findings the operator intentionally excluded.
- **Do not copy `_ANALYZER_PREFIXES`** into `provenance.py` — import `is_analyzer_source` from `scope.py` to keep a single source of truth.
- **Do not call private `_is_analyzer`** from `provenance.py` — use the new public `is_analyzer_source(source: str)`.
- **Do not add `provenance.py` to any `__init__.py` re-exports** unless one already exists for `findings/` (none does).

### Project Structure Notes

- New file: `ai_pr_review/findings/provenance.py` (consistent with existing `suppress.py`, `scope.py`, `merge.py` — single-responsibility modules).
- New file: `tests/python/test_provenance.py` (flat file alongside existing `test_findings.py`, `test_scope.py`).
- No other directories touched.

### References

- GitHub issues: #360 (Epic 7, parent), #361 (Epic 6, parent), #356 (closed: AI_TEMPERATURE), #357 (closed: max-token clamp), #358 (closed: CI concurrency)
- Architecture: `memory-bank/bmad/planning-artifacts/architecture-ai-pr-review.md#Findings Pipeline` (lines 362-390)
- Finding model: `ai_pr_review/findings/models.py`
- Merge logic: `ai_pr_review/findings/merge.py`
- Scope predicate: `ai_pr_review/findings/scope.py` (lines 35-65)
- Agent names: `ai_pr_review/agents/roster.py` (line 176)
- Test style reference: `tests/python/test_findings.py` (lines 17-26, 239-246)

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

### Completion Notes List

- Implemented `is_analyzer_source(source: str) -> bool` in `scope.py` as a public single-string predicate; refactored `_is_analyzer` to delegate to it (behavior unchanged, 40 scope tests still green).
- Added `corroborated: bool = False` field to `Finding` after `out_of_diff`; `to_dict()` untouched — field is internal-only like `out_of_diff`.
- Created `ai_pr_review/findings/provenance.py` with `PROVENANCE_BOOST=10`, `is_corroborated()`, `boosted_confidence()`. No import cycle: `merge → provenance → {scope, roster}`.
- Updated `merge._collapse_cluster` to use `dict[str, object]` update dict (required for mypy), set `corroborated=True` and boost confidence when `is_corroborated(all_sources)`.
- Ruff I001 (import order) auto-fixed in `test_provenance.py`.
- Validation results: 1606 pytest tests pass, mypy clean (83 source files), ruff clean.

### File List

- `ai_pr_review/findings/scope.py` (modified)
- `ai_pr_review/findings/models.py` (modified)
- `ai_pr_review/findings/provenance.py` (new)
- `ai_pr_review/findings/merge.py` (modified)
- `tests/python/test_provenance.py` (new)
- `tests/python/test_findings.py` (modified)
