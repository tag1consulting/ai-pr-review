# Story 4.2: Telemetry Hooks

Status: review

## Story

As an **operator running the Python review engine in production**,
I want optional structured telemetry events emitted after each review run to a local file or HTTP endpoint,
so that I can monitor token costs, latency trends, finding rates, and feedback loop activity across many PRs without digging through log lines.

## Acceptance Criteria

1. When `AI_TELEMETRY_ENABLED=false` (default) no telemetry file is written and no HTTP request is made — zero overhead on the hot path.
2. When `AI_TELEMETRY_ENABLED=true` and `AI_TELEMETRY_SINK=file:///path/to/telemetry.jsonl`, one JSON object is appended per review run (newline-delimited, append mode).
3. When `AI_TELEMETRY_ENABLED=true` and `AI_TELEMETRY_SINK=https://...`, one HTTP POST with `Content-Type: application/json` is made per review run; failure is logged at WARNING and silently swallowed — telemetry must never abort the review.
4. The emitted JSON object contains exactly: `correlation_id`, `timestamp` (ISO-8601 UTC), `repository`, `pr_number`, `outcome` (event name string), `findings_count`, `findings_by_severity` (dict), `failed_agents` (list of names), `token_usage_by_agent` (dict agent→{input,output,cache_creation,cache_read,model}), `agent_latency_ms` (dict agent→int), `sarif_elapsed_s` (float or null), `learning_store_entries_loaded` (int), `telemetry_schema_version` (string `"1"`).
5. `AI_TELEMETRY_ENABLED` and `AI_TELEMETRY_SINK` are added to `_KNOWN_AI_VARS` in `config.py` and parsed in `ReviewConfig.from_env()`.
6. `ReviewConfig` has `telemetry_enabled: bool = False` and `telemetry_sink: str = ""` fields.
7. When `telemetry_enabled=True` and `telemetry_sink` is empty or unrecognised scheme, a WARNING is logged and telemetry is skipped (not an error).
8. `tests/python/test_telemetry.py` covers: file sink write, HTTP POST call, disabled=no side effects, failed HTTP silently swallowed, empty sink skipped, schema version field present.
9. No third-party HTTP library added — use `httpx` which is already a project dependency (see `pyproject.toml`).

## Tasks / Subtasks

- [x] Task 1: Add config fields (AC: 5, 6)
  - [x] Add `telemetry_enabled: bool = False` and `telemetry_sink: str = ""` to `ReviewConfig`
  - [x] Add `AI_TELEMETRY_ENABLED` and `AI_TELEMETRY_SINK` to `_KNOWN_AI_VARS`
  - [x] Wire in `ReviewConfig.from_env()`: `telemetry_enabled=_bool("AI_TELEMETRY_ENABLED", False)`, `telemetry_sink=os.environ.get("AI_TELEMETRY_SINK", "")`
  - [x] Add `_bool()` helper if not already present (check config.py — `_int()` exists, `_bool()` may not) — already present from E4.S1

- [x] Task 2: Create `ai_pr_review/telemetry.py` (AC: 1, 2, 3, 4, 7, 9)
  - [x] Define `TelemetryEvent` dataclass with all fields from AC-4
  - [x] Implement `emit_telemetry(event: TelemetryEvent, *, sink: str) -> None` — routes to `_emit_file` or `_emit_http` based on `sink` prefix; warns + returns on empty/bad sink
  - [x] `_emit_file(event, path)` — opens in append mode (`"a"`), writes `json.dumps(asdict(event)) + "\n"`; wraps in try/except OSError
  - [x] `_emit_http(event, url)` — synchronous `httpx.post(url, json=asdict(event), timeout=5.0)`; wraps in broad try/except; logs WARNING on failure

- [x] Task 3: Wire telemetry into `cli.py` `_run_review_async` (AC: 1, 4)
  - [x] After `_emit_review_result(result, ...)`, if `config.telemetry_enabled`, build `TelemetryEvent` from `result` + context and call `emit_telemetry`
  - [x] Populate `token_usage_by_agent` from `result.agent_results` (each `AgentResult.token_log`)
  - [x] Populate `agent_latency_ms` — empty dict `{}` for now; E4.S4 will populate it
  - [x] Populate `sarif_elapsed_s` — wired from `result.sarif_elapsed_s` (E4.S3 already landed)
  - [x] Populate `learning_store_entries_loaded` from `feedback_entries_count` (pre-computed before feedback block)
  - [x] Populate `findings_by_severity` from `result.findings` using `f.severity` field
  - [x] Populate `outcome` from `result.outcome.event`
  - [x] Populate `repository` from `config.github_repository`
  - [x] Populate `pr_number` from `str(config.pr_number)`
  - [x] Wrap entire telemetry block in `try/except Exception` — telemetry must never crash the review

- [x] Task 4: Write tests `tests/python/test_telemetry.py` (AC: 8)
  - [x] `test_file_sink_writes_json` — call `emit_telemetry` with `file://` sink pointing to `tmp_path`; assert file contains valid JSON with expected fields
  - [x] `test_http_sink_posts_json` — monkeypatch `httpx.post`; assert called with correct URL and JSON body
  - [x] `test_http_failure_swallowed` — monkeypatch `httpx.post` to raise `httpx.NetworkError`; assert no exception raised
  - [x] `test_empty_sink_skipped` — call `emit_telemetry` with empty sink; assert WARNING logged
  - [x] `test_schema_version_field` — assert emitted JSON has `telemetry_schema_version == "1"`
  - [x] `test_config_fields_from_env` — set `AI_TELEMETRY_ENABLED=true`, `AI_TELEMETRY_SINK=file:///tmp/t.jsonl`; assert `ReviewConfig.from_env()` parses correctly

- [x] Task 5: Run full quality gate
  - [x] `pytest tests/python/ -q` — 859 tests pass
  - [x] `mypy ai_pr_review/telemetry.py ai_pr_review/config.py ai_pr_review/cli.py --strict`
  - [x] `ruff check ai_pr_review/telemetry.py ai_pr_review/config.py ai_pr_review/cli.py`

## Dev Notes

### Critical architecture constraints

**Telemetry must be fail-soft everywhere.** The `emit_telemetry()` call in `cli.py` must be wrapped in `try/except Exception` so a broken sink, serialization error, or import failure can never abort a review. Mirror the pattern used for `_run_summarizer()` and `_upsert_token_table()` in `cli.py` — both already follow this fail-soft pattern.

**No new HTTP dependency.** `httpx` is already a direct dependency (see `pyproject.toml` — it's used in `ai_pr_review/llm/_http.py`). Use `httpx.post()` synchronously (not async) in `_emit_http`. The telemetry call happens after the review is complete and outside the anyio event loop, so synchronous is correct.

**`AgentResult` does not have `elapsed_ms`.** `FailedAgent` has it (dispatch.py:50), but `AgentResult` does not. Do NOT add `elapsed_ms` to `AgentResult` in this story — that's E4.S4 scope. Emit `agent_latency_ms: {}` (empty dict) for now and note in a TODO comment that E4.S4 will populate it.

**`sarif_elapsed_s` is now available.** E4.S3 landed before E4.S2 and has already changed `load_sarif_files()` to return `(findings, elapsed)`, threading the value through `orchestrate.py` → `ReviewResult.sarif_elapsed_s`. Wire it directly from `result.sarif_elapsed_s` in `_run_review_async` — do not emit `null`. The field is present and populated when SARIF paths are configured; it is `None` when no SARIF paths are provided.

### Repository and PR number in config

Check `config.py` for fields that hold these values. The relevant fields are likely `pr_number: int` and a repo string. Search with:
```bash
grep -n "pr_number\|github_repository\|repo\|GITHUB_REPOSITORY" ai_pr_review/config.py
```
Use `str(config.pr_number)` and the repo field as-is. If `pr_number` is 0 (compute-only path), that's fine — telemetry only fires from the `review` subcommand.

### `_bool()` helper in config.py

Check whether a `_bool()` helper exists in `config.py`. The file has `_int()` for integer parsing. If `_bool()` is absent, add:
```python
def _bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")
```
Place it alongside `_int()`.

### `findings_by_severity` calculation

`result.findings` is `list[Finding]`. Each `Finding` has a `severity` field (string). Build the dict with:
```python
from collections import Counter
findings_by_severity = dict(Counter(f.severity for f in result.findings))
```

### `learning_store_entries_loaded`

In `_run_review_async`, the variable `entries` is set at line ~184:
```python
entries = store.load_recent()
```
This is inside an `if config.enable_feedback_loop:` block. If feedback loop is disabled, `entries` is never assigned. Use:
```python
learning_store_entries_loaded=len(entries) if config.enable_feedback_loop else 0,
```

### File sink path parsing

`AI_TELEMETRY_SINK=file:///absolute/path` — strip the `file://` prefix to get the path:
```python
path = sink[len("file://"):]
```
`file:///tmp/t.jsonl` → path = `/tmp/t.jsonl`. This is correct for absolute paths.

### `TelemetryEvent` serialization

Use `@dataclass` (not frozen — `asdict()` needs mutable). Use `dataclasses.asdict()` for JSON serialization. The `timestamp` field should be a `str` (pre-formatted ISO-8601), not a `datetime` object, so `asdict()` produces JSON-serializable output directly.

### Where to place the telemetry call in cli.py

After the `_emit_review_result(result, ...)` call at the end of `_run_review_async`. Example structure:
```python
_emit_review_result(result, base_ref=base_ref, head=head_sha)

if config.telemetry_enabled:
    try:
        from ai_pr_review.telemetry import TelemetryEvent, emit_telemetry
        # ... build event ...
        emit_telemetry(event, sink=config.telemetry_sink)
    except Exception as exc:
        logger.warning("[ai-pr-review] WARNING: telemetry emission failed: %s", exc)
```

### Project structure notes

- New file: `ai_pr_review/telemetry.py`
- New test file: `tests/python/test_telemetry.py`
- Modified: `ai_pr_review/config.py` (two new fields + two new `_KNOWN_AI_VARS` entries + `from_env()`)
- Modified: `ai_pr_review/cli.py` (telemetry call after `_emit_review_result`)

### References

- `ai_pr_review/config.py` — `_KNOWN_AI_VARS`, `_int()`, `ReviewConfig`, `from_env()` [Source: ai_pr_review/config.py]
- `ai_pr_review/cli.py:406–468` — `_upsert_token_table` fail-soft pattern [Source: ai_pr_review/cli.py]
- `ai_pr_review/agents/dispatch.py:23–55` — `TokenUsage`, `AgentResult`, `FailedAgent` dataclasses [Source: ai_pr_review/agents/dispatch.py]
- `ai_pr_review/orchestrate.py:47–67` — `ReviewResult` fields [Source: ai_pr_review/orchestrate.py]
- `ai_pr_review/llm/_http.py` — existing `httpx` usage pattern [Source: ai_pr_review/llm/_http.py]
- `ai_pr_review/logging.py` — E4.S1 logging module (dependency, already merged) [Source: ai_pr_review/logging.py]
- PRD 4.FR-2 [Source: memory-bank/bmad/planning-artifacts/prd-ai-pr-review.md#4.FR-2]

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

### Completion Notes List

- Created `ai_pr_review/telemetry.py` with `TelemetryEvent` dataclass (13 fields, schema version "1") and `emit_telemetry()` routing to `_emit_file` (append mode) or `_emit_http` (synchronous httpx.post, timeout=5s). All errors are fail-soft: OSError in file sink and any exception in HTTP sink are logged as WARNING and swallowed.
- Added `telemetry_enabled: bool = False` and `telemetry_sink: str = ""` fields to `ReviewConfig` with `AI_TELEMETRY_ENABLED` and `AI_TELEMETRY_SINK` in `_KNOWN_AI_VARS`. `_bool()` helper already existed from E4.S1.
- Wired telemetry call after `_emit_review_result` in `_run_review_async` in `cli.py`. Pre-initialized `feedback_entries_count = 0` before the feedback loop block so telemetry can reference it without scope issues. `agent_latency_ms` is `{}` (E4.S4 scope). `sarif_elapsed_s` wired from `result.sarif_elapsed_s` (available since E4.S3 landed first).
- 11 tests in `test_telemetry.py` covering: file sink write, file sink append (two calls), HTTP post, HTTP failure swallowed, HTTP timeout swallowed, empty sink WARNING, unknown scheme WARNING, schema version field, all required fields present, config fields from env, OSError swallowed.
- 859 tests pass; mypy --strict and ruff clean on all 3 modified/created source files.

### File List

- `ai_pr_review/telemetry.py` (created)
- `ai_pr_review/config.py` (modified — two new fields, two new _KNOWN_AI_VARS, from_env() wiring)
- `ai_pr_review/cli.py` (modified — telemetry call after _emit_review_result, feedback_entries_count pre-init)
- `tests/python/test_telemetry.py` (created)
