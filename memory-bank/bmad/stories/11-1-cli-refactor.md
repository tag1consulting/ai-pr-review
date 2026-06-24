# Story 11.1: Extract Preflight and Reporting from cli._run_review_async

**Epic:** 11 — CLI Decomposition
**Story ID:** 11-1
**Story Key:** 11-1-cli-refactor
**GitHub Issue:** #497
**Status:** ready-for-dev

---

## Story

As a **contributor**,
I want `_run_review_async` decomposed into named helpers and `_emit_telemetry` collapsed to a single function,
so that `cli.py` is easy to read and extend without tracing through a 130-line monolith and two near-identical telemetry paths.

---

## Acceptance Criteria

1. `_run_review_async` shrinks to a sequence of named phase calls; no individual phase logic lives inline in the function body.
2. `_emit_telemetry` and `_emit_telemetry_minimal` are collapsed into a single `_emit_telemetry` that accepts `result: ReviewResult | None`; when `None`, all agent-dependent fields default to zero/empty. All three call sites (skip, dry-run, normal) use the unified function.
3. `_build_token_table_accordion` and `_write_step_summary` move to `ai_pr_review/review/reporting.py`; `cli.py` imports and delegates to them. Both functions keep their existing signatures and fail-soft behavior.
4. `_run_summarizer` and `_run_issue_linker` (the preflight LLM calls) move to `ai_pr_review/review/preflight.py`; `cli.py` calls them via that module. `_orchestrate_skip` stays in `cli.py` (it constructs a fake run-review context and belongs near the CLI entry point).
5. Full Python suite green (`pytest tests/python -q`); `mypy ai_pr_review/` clean; `ruff check ai_pr_review/ tests/python/` clean.
6. No behavior change: same log lines, same telemetry events, same step-summary content, same VCS posts.
7. No new config knobs, env vars, or public API surface. New module functions are module-private (leading `_` or unexported from `__init__`).

---

## Technical Notes

### Current structure of `_run_review_async` (lines 138–275, cli.py)

The function currently does all of the following in sequence:

1. Build `ReviewRuntime` via `build_review_runtime()`
2. Run preflight: `_run_summarizer` + `_run_issue_linker` (both async, interleaved with anyio)
3. Early-return skip path (calls `_orchestrate_skip`)
4. Build `token_table_renderer` closure (captures `config` + `runtime`)
5. Early-return dry-run path (calls `_emit_telemetry_minimal`)
6. Call `run_review(...)` (the core orchestrator)
7. Write GitHub step summary (`_write_step_summary`)
8. Emit telemetry (`_emit_telemetry`)

### Telemetry collapse

`_emit_telemetry` (lines 278–344) iterates `result.agent_results` for per-agent token counts. `_emit_telemetry_minimal` (lines 346–387) is the same function body with all those fields set to zero/empty defaults.

Target: unified signature `_emit_telemetry(config, result: ReviewResult | None, *, outcome_override, is_incremental)`. When `result is None`, skip the agent-result loop; emit zeros for all token fields.

### New module layout

| New file | What moves there |
|----------|-----------------|
| `ai_pr_review/review/preflight.py` | `_run_summarizer`, `_run_issue_linker`, `_fetch_open_issues`, `_SUMMARIZER_FAILURE_NOTICE` |
| `ai_pr_review/review/reporting.py` | `_build_token_table_accordion`, `_write_step_summary`, `_emit_review_result` |

`_orchestrate_skip` stays in `cli.py` (depends on deferred imports inside the function body that avoid circular imports at import time; no reason to move it).

### Import discipline

`cli.py` uses deferred `from X import Y` inside function bodies to prevent circular imports. The new modules must follow the same pattern: do not add top-level imports of `ai_pr_review.agents.*` or `ai_pr_review.orchestrate.*` to `preflight.py` or `reporting.py`.

### Token table closure

`_build_token_table_accordion` is currently passed as the `token_table_renderer` callback to `run_review()`. After the move to `reporting.py`, the closure in `_run_review_async` simply captures `config` and calls `reporting._build_token_table_accordion(...)`. The function signature is unchanged.

---

## Tasks

- [ ] Create `ai_pr_review/review/preflight.py`; move `_run_summarizer`, `_run_issue_linker`, `_fetch_open_issues`, `_SUMMARIZER_FAILURE_NOTICE` there; update `cli.py` imports.
- [ ] Create `ai_pr_review/review/reporting.py`; move `_build_token_table_accordion`, `_write_step_summary`, `_emit_review_result` there; update `cli.py` imports.
- [ ] Collapse `_emit_telemetry` and `_emit_telemetry_minimal` into a single function with `result: ReviewResult | None`; remove `_emit_telemetry_minimal`; update all three call sites.
- [ ] Slim `_run_review_async` to a sequence of named phase calls.
- [ ] Run full suite + mypy + ruff; fix any issues.
- [ ] Update `CLAUDE.md` module table to add `preflight.py` and `reporting.py` rows.

---

## Dev Notes

- `anyio` is already imported at the top of `cli.py`. `preflight.py` will need it for `anyio.to_thread.run_sync` in `_run_summarizer` and `_run_issue_linker`.
- `_emit_review_result` (line 860) is a one-liner stderr echo; moving it to `reporting.py` is optional but keeps all result-display code together.
- The `tests/python/test_cli.py` file covers the skip and dry-run paths; after the refactor those paths will still exercise the unified `_emit_telemetry` via the same call sites.
- No existing test file covers `_build_token_table_accordion` or `_write_step_summary` directly; if coverage is thin, add smoke tests in a new `tests/python/review/test_reporting.py`.
