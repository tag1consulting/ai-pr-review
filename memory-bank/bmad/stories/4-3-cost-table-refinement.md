# Story 4.3 — Cost Table Refinement

**Epic:** 4 — Soak, Observability, Default Flip
**Story ID:** 4-3
**Story Key:** 4-3-cost-table-refinement
**GitHub Issue:** #243
**Status:** ready-for-dev
**PRD refs:** 4.FR-3

---

## User Story

As an **operator monitoring review costs**, I want the token cost table to include context-enrichment token counts, SARIF ingestion wall time, and per-agent output-token caps, so that I can see the full cost of each review run and identify agents that hit their output ceiling.

---

## Acceptance Criteria

- [ ] When `AI_CONTEXT_ENRICHMENT=1` and context tokens > 0, a "Context enrichment" supplementary row appears below the agent rows showing the token count used for the `<symbol-context>` block
- [ ] When SARIF paths are provided and ingestion runs, a "SARIF ingestion" supplementary row appears showing wall-clock time in seconds (2 decimal places, e.g. `0.34s`)
- [ ] Per-agent rows in the table show the `max_output_tokens` cap alongside actual output tokens in the format `actual / cap` (e.g. `1234 / 16384`) in the Output column
- [ ] When context enrichment is disabled or token count is zero, the "Context enrichment" row does not appear
- [ ] When no SARIF paths are provided, the "SARIF ingestion" row does not appear
- [ ] Existing table structure (columns, Total row, cache columns, `**bold**` Total row) is preserved exactly — new rows are additive only
- [ ] `tests/python/test_pricing.py` extended with tests for all three new behaviors

---

## Current Cost Table Format

`emit_token_table()` in `ai_pr_review/pricing.py` currently renders a markdown table with two layouts:

**Without cache tokens (6 columns):**
```
| Agent | Model | Input | Output | Total | Est. Cost |
|-------|-------|------:|-------:|------:|----------:|
| code-reviewer | Sonnet 4.6 | 1000 | 500 | 1500 | $0.0082 |
| **Total** | | **1000** | **500** | **1500** | **$0.0082** |
```

**With cache tokens (8 columns):**
```
| Agent | Model | Input | Output | Cache Write | Cache Read | Total | Est. Cost |
|-------|-------|------:|-------:|------------:|-----------:|------:|----------:|
| security-reviewer | Sonnet 4.6 | 1000 | 500 | 200 | 50 | 1750 | $0.0097 |
| **Total** | | **1000** | **500** | **200** | **50** | **1750** | **$0.0097** |
```

Key facts from reading the source:
- `emit_token_table(token_log, pricing_data)` takes two arguments today
- `any_cache` flag switches between 6-column and 8-column layouts
- Per-agent rows built from `TokenEntry(agent, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)`
- `TokenEntry` is a `@dataclass` — `max_output_tokens` is **not** currently a field
- Total row uses `**bold**` markdown and aggregates all per-entry totals
- `cost_display = "n/a"` when model rates are unknown; `any_unknown` appends `+` to grand total

---

## Required Changes

### 1. `ai_pr_review/pricing.py`

**A. Add `max_output_tokens` to `TokenEntry`:**

```python
@dataclass
class TokenEntry:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    max_output_tokens: int = 0   # NEW — 0 means "no cap shown"
```

A value of `0` means the cap is unknown or should not be displayed (backwards-compatible default).

**B. Change Output column rendering in per-agent rows:**

When `entry.max_output_tokens > 0`, render the Output cell as `f"{entry.output_tokens} / {entry.max_output_tokens}"` instead of plain `f"{entry.output_tokens}"`. When `max_output_tokens == 0`, render as before.

**C. Extend `emit_token_table()` signature with new optional parameters:**

```python
def emit_token_table(
    token_log: list[TokenEntry],
    pricing_data: list[dict[str, object]],
    *,
    context_tokens: int = 0,
    sarif_elapsed_s: float | None = None,
) -> str:
```

- `context_tokens`: token count from context enrichment (0 = row omitted)
- `sarif_elapsed_s`: wall-clock seconds for SARIF ingestion (`None` = row omitted)

**D. Append supplementary rows after the Total row:**

After the existing Total row, conditionally append:

```
| Context enrichment | *(context)* | {context_tokens} | — | — | — |
```
(or the 8-column variant when `any_cache` is True)

```
| SARIF ingestion | *(timing)* | — | {sarif_elapsed_s:.2f}s | — | — |
```

The supplementary rows are purely informational — they do **not** contribute to the grand Total cost calculation and do not affect the `total_in`/`total_out` accumulators. Use `—` for cells that don't apply (cost cell = `—` for both supplementary rows).

Exact column widths/alignment for supplementary rows must match the existing header separator. Use `*(context)*` and `*(timing)*` in the Model column as a visual cue that these are not agent entries.

### 2. `ai_pr_review/agents/dispatch.py`

No structural changes needed. `AgentResult.token_log` is a `TokenUsage` — the caller (CLI / orchestrate) is responsible for translating `TokenUsage` → `TokenEntry`. See threading path below.

### 3. `ai_pr_review/orchestrate.py` and CLI caller

The orchestrator currently does **not** call `emit_token_table()` directly — that is done by the CLI layer. The CLI caller (wherever it builds `token_log: list[TokenEntry]`) must:

1. Populate `TokenEntry.max_output_tokens` from the agent's `AgentSpec.max_output_tokens` when building each `TokenEntry` from `AgentResult.token_log`. Look up the spec by agent name via `roster.get_agent(result.name)`.

2. Pass `context_tokens` from the context enrichment pipeline. The token count is the length (in estimated tokens) of the `<symbol-context>` block that was prepended, **not** the budget ceiling. Source: the return value of `build_context_block()` in `ai_pr_review/context/budget.py` — specifically `_estimate_tokens(ctx_block)` applied to the returned string. The dispatch layer currently discards this value; it must be threaded upward. See threading notes below.

3. Pass `sarif_elapsed_s` from SARIF ingestion. See sarif.py changes below.

**Context token threading:** `_build_user_message()` in `dispatch.py` calls `build_context_block()` but discards the returned string's token count. The simplest extension is to have `_build_user_message()` return a tuple `(message: str, context_token_count: int)` and accumulate the maximum (or sum) across agents in `run_tier()`. Alternatively, the CLI can re-estimate from the diff. **Recommended approach:** add a `context_tokens_used: int` field to `AgentResult` (default 0) and populate it in `_run_single_agent()` when enrichment fires. The CLI then sums (or takes max, since all enriched agents get the same block) across `AgentResult` entries.

### 4. `ai_pr_review/analyzers/sarif.py`

Add wall-clock timing to `load_sarif_files()`:

```python
import time

def load_sarif_files(paths: list[str]) -> tuple[list[Finding], float]:
    """Parse all SARIF files in *paths*.

    Returns ``(findings, elapsed_seconds)``.  ``elapsed_seconds`` is the
    wall-clock time for the full ingestion pass (0.0 if *paths* is empty).
    """
    t0 = time.monotonic()
    all_findings: list[Finding] = []
    for path in paths:
        file_findings = _parse_sarif_file(path)
        logger.info("SARIF: %r → %d finding(s)", path, len(file_findings))
        all_findings.extend(file_findings)
    return all_findings, time.monotonic() - t0
```

The return type changes from `list[Finding]` to `tuple[list[Finding], float]`. Update the call site in `orchestrate.py` (Phase 1.5 block):

```python
sarif_findings, sarif_elapsed_s = load_sarif_files(list(cfg.sarif_paths))
```

Thread `sarif_elapsed_s` through `ReviewResult` or directly to the cost-table call in the CLI.

### 5. `ai_pr_review/agents/roster.py`

No changes required. `AgentSpec.max_output_tokens` is already present on every entry (all currently set to `16384`). The comment on line 64 already notes this field is used for cost-table rendering (`2.NFR-4`). The CLI caller reads it via `roster.get_agent(name).max_output_tokens`.

---

## Example Output

### Before (current, no new features):

```
| Agent | Model | Input | Output | Total | Est. Cost |
|-------|-------|------:|-------:|------:|----------:|
| code-reviewer | Sonnet 4.6 | 1000 | 500 | 1500 | $0.0082 |
| security-reviewer | Sonnet 4.6 | 800 | 300 | 1100 | $0.0057 |
| **Total** | | **1800** | **800** | **2600** | **$0.0139** |
```

### After (all new features enabled):

```
| Agent | Model | Input | Output | Total | Est. Cost |
|-------|-------|------:|-------:|------:|----------:|
| code-reviewer | Sonnet 4.6 | 1000 | 500 / 16384 | 1500 | $0.0082 |
| security-reviewer | Sonnet 4.6 | 800 | 300 / 16384 | 1100 | $0.0057 |
| **Total** | | **1800** | **800** | **2600** | **$0.0139** |
| Context enrichment | *(context)* | 2048 | — | — | — |
| SARIF ingestion | *(timing)* | — | 0.34s | — | — |
```

Notes on the example:
- The `output_tokens` field in `TokenEntry` stores the raw integer; the `/ cap` suffix is rendered by `emit_token_table()` only
- The Total row Output column sums raw integers (800), not the formatted strings
- Supplementary rows use the same column count as the rest of the table but have `—` for inapplicable cells
- SARIF elapsed is shown in the Output column for the timing row (the most readable placement given existing column semantics)

---

## Test Requirements

Extend `tests/python/test_pricing.py`. All tests must pass with `pytest tests/python/test_pricing.py`.

### New tests to add:

**`test_emit_token_table_max_output_tokens_shown`**
- Build a `TokenEntry` with `max_output_tokens=16384` and `output_tokens=1234`
- Assert the table contains `"1234 / 16384"` in output
- Assert `"**Total**"` row still contains `"**1234**"` (raw integer, not the formatted string)

**`test_emit_token_table_max_output_tokens_zero_omitted`**
- Build a `TokenEntry` with `max_output_tokens=0` (default)
- Assert the table does NOT contain `" / "` in the output column

**`test_emit_token_table_context_enrichment_row`**
- Call `emit_token_table(log, pricing_data, context_tokens=2048)`
- Assert `"Context enrichment"` appears in the table
- Assert `"2048"` appears in the table
- Assert `"*(context)*"` appears in the table

**`test_emit_token_table_context_enrichment_zero_omitted`**
- Call `emit_token_table(log, pricing_data, context_tokens=0)` (default)
- Assert `"Context enrichment"` does NOT appear

**`test_emit_token_table_sarif_row`**
- Call `emit_token_table(log, pricing_data, sarif_elapsed_s=0.34)`
- Assert `"SARIF ingestion"` appears
- Assert `"0.34s"` appears
- Assert `"*(timing)*"` appears

**`test_emit_token_table_sarif_none_omitted`**
- Call `emit_token_table(log, pricing_data, sarif_elapsed_s=None)` (default)
- Assert `"SARIF ingestion"` does NOT appear

**`test_emit_token_table_supplementary_rows_not_in_total_cost`**
- Build two `TokenEntry` rows with known costs
- Call with `context_tokens=500, sarif_elapsed_s=1.5`
- Verify the Total row cost is unchanged vs. calling without those kwargs
- Verify `total_in`/`total_out` in the Total row are unaffected by supplementary rows

**`test_load_sarif_files_returns_elapsed`** (in `tests/python/test_sarif.py` or a new test — check if the file exists first)
- Assert `load_sarif_files([])` returns `([], elapsed)` where `elapsed >= 0.0`
- Assert `load_sarif_files(["nonexistent.sarif"])` returns `([], elapsed)` (fail-soft + timing)

---

## Files Modified

| File | Change |
|---|---|
| `ai_pr_review/pricing.py` | Add `max_output_tokens` to `TokenEntry`; extend `emit_token_table()` with `context_tokens` and `sarif_elapsed_s` kwargs; render cap in Output column; append supplementary rows |
| `ai_pr_review/analyzers/sarif.py` | Add `time.monotonic()` timing to `load_sarif_files()`; change return type to `tuple[list[Finding], float]` |
| `ai_pr_review/orchestrate.py` | Unpack `(sarif_findings, sarif_elapsed_s)` from `load_sarif_files()`; thread `sarif_elapsed_s` to wherever cost table is rendered |
| `ai_pr_review/agents/dispatch.py` | Add `context_tokens_used: int = 0` to `AgentResult`; populate in `_run_single_agent()` when enrichment fires |
| `tests/python/test_pricing.py` | Add 7 new test functions (see above) |
| `tests/python/test_sarif.py` | Add `test_load_sarif_files_returns_elapsed` (check file exists first) |
| `memory-bank/bmad/implementation-artifacts/sprint-status.yaml` | `4-3-cost-table-refinement: ready-for-dev → in-progress` when dev starts |

---

## Dev Agent Guardrails

- **Do not create a new rendering path.** All table rendering flows through `emit_token_table()` in `pricing.py`. No parallel rendering function.
- **Additive only.** The existing table format must be byte-for-byte identical when `context_tokens=0` and `sarif_elapsed_s=None` and all `max_output_tokens=0`. Write a regression test that asserts this.
- **Backwards compatibility on `load_sarif_files`.** The return type change from `list[Finding]` to `tuple[list[Finding], float]` is a breaking change to a public function. Update every call site in the same PR. Search for `load_sarif_files` across the codebase before submitting — there may be tests that call it directly.
- **`max_output_tokens=0` means "do not show cap".** Never render `"/ 0"` in the output column. The guard is `if entry.max_output_tokens > 0`.
- **Total row accumulates raw `output_tokens`, not formatted strings.** The `/ cap` suffix is presentation-only. The `total_out` accumulator must use `entry.output_tokens` (the integer), not the formatted cell string.
- **SARIF elapsed is `None`, not `0.0`, when SARIF is not used.** A 0.0 elapsed with no paths would be confusing. Keep `None` as the sentinel for "row omitted".
- **Supplementary rows do not affect `total_cost`.** The Total row cost must be identical whether supplementary rows are shown or not.
- **Check for `test_sarif.py` before adding to it.** Run `find /home/gchaix/repos/tag1/ai-pr-review/tests -name 'test_sarif*'` to locate the file. If it does not exist, add the timing test to `test_pricing.py` or a new `test_sarif.py` as appropriate.
- **Run the full test suite before marking done:** `cd /home/gchaix/repos/tag1/ai-pr-review && python -m pytest tests/python/ -q`
- **Run mypy and ruff before marking done:**
  ```bash
  cd /home/gchaix/repos/tag1/ai-pr-review
  python -m mypy ai_pr_review/pricing.py ai_pr_review/analyzers/sarif.py ai_pr_review/agents/dispatch.py ai_pr_review/orchestrate.py
  python -m ruff check ai_pr_review/pricing.py ai_pr_review/analyzers/sarif.py ai_pr_review/agents/dispatch.py ai_pr_review/orchestrate.py
  ```
- **No print() for new code.** Use `logging.getLogger(__name__)` for any diagnostic messages. `pricing.py` currently uses `print(..., file=sys.stderr)` for pricing-file warnings — match that existing pattern there, but do not add new `print()` calls for the new rendering logic.
- **Do not touch `review.sh` or any bash files.** This story is Python-only.
