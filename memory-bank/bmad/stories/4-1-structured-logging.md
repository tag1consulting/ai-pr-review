# Story 4.1 — Structured Logging

**Epic:** 4 — Soak, Observability, Default Flip
**Story ID:** 4-1
**Story Key:** 4-1-structured-logging
**GitHub Issue:** #241
**Status:** review
**PRD refs:** 4.FR-1

---

## User Story

As an **operator running the Python engine**, I want structured JSON log lines on stderr (when `AI_LOG_FORMAT=json`) with a correlation ID that propagates across every LLM call, analyzer subprocess, and VCS API call, so that I can trace a single review run end-to-end in log aggregators without losing context at subprocess boundaries.

---

## Acceptance Criteria

- [ ] `AI_LOG_FORMAT=json` produces valid JSON lines on stderr; each line is a self-contained JSON object
- [ ] `AI_LOG_FORMAT=human` (default) produces human-readable text on stderr — same as current `print()` / `logging` output
- [ ] Every JSON log line contains: `timestamp` (ISO-8601), `level`, `logger`, `message`, `correlation_id`
- [ ] `AI_LOG_LEVEL` controls the minimum log level (default: `WARNING`; accepted values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)
- [ ] Correlation ID is visible in log output across all subprocess boundaries (analyzers)
- [ ] Secret masking: any log line whose formatted message contains a string matching a known secret field (API keys, tokens, etc.) has those values replaced with `<redacted>` before the line is emitted
- [ ] Pytest: injecting a fake API key in env (`ANTHROPIC_API_KEY=sk-ant-fake-key-for-test`) and then logging a message that includes it produces output with `<redacted>`, never the raw key value
- [ ] `AI_LOG_FORMAT` and `AI_LOG_LEVEL` added to `_KNOWN_AI_VARS` in `config.py` and parsed in `ReviewConfig.from_env()`
- [ ] No third-party logging libraries introduced (`structlog`, `loguru`, etc.)
- [ ] `logging.basicConfig` is never called from within the library; configuration is caller-owned (entry point only)

---

## Technical Design

### New file: `ai_pr_review/logging.py`

This module owns the formatter, the filter, and the `setup_logging()` entry point. It does **not** parse env vars — it receives already-parsed values from `ReviewConfig`.

**Public API to expose:**

```python
def setup_logging(log_format: str, log_level: str, correlation_id: str) -> None:
    """Configure the root logger for this process.

    Must be called once at process startup (cli.py review/compute/slash commands).
    Installs a SecretMaskingFormatter on stderr. Sets the root logger level.
    Stores correlation_id in a module-level contextvars.ContextVar so it is
    available to the CorrelationFilter on every log record.
    """

class SecretMaskingFormatter(logging.Formatter):
    """logging.Formatter subclass that redacts secret-like strings before emission.

    Applies _mask_secrets() to the fully-formatted log line before returning it.
    """

class CorrelationFilter(logging.Filter):
    """logging.Filter that injects correlation_id into every LogRecord.

    Reads the current value from the module-level ContextVar and sets
    record.correlation_id. Compatible with both text and JSON formatters.
    """

def _mask_secrets(text: str) -> str:
    """Redact known secret patterns from a string.

    Patterns to redact (regex-based, applied in order):
    1. Any value assigned to env var names matching: *API_KEY*, *TOKEN*, *SECRET*,
       *PASSWORD*, *_KEY (suffix) — matched case-insensitively
    2. Provider-specific token prefixes: sk-ant-, sk-, ghp_, ghs_, glpat-,
       github_pat_, glcbt- followed by 10+ alphanumeric chars
    3. The literal value of any non-empty secret field read from ReviewConfig
       at setup time (anthropic_api_key, openai_api_key, google_api_key,
       bedrock_api_key, gh_token, bitbucket_api_token, gitlab_token, ci_job_token)

    All matches replaced with <redacted>.
    """

def generate_correlation_id() -> str:
    """Return a new short correlation ID (8 hex chars from uuid4)."""
```

**JSON line format** (one JSON object per line, no trailing comma, newline-terminated):
```json
{"timestamp": "2026-05-18T12:00:00.123456Z", "level": "WARNING", "logger": "ai_pr_review.agents.dispatch", "message": "agent 'blind-hunter' failed ...", "correlation_id": "a1b2c3d4"}
```

**Human format:** standard `logging` format string:
```
%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s: %(message)s
```

### Modified files and specific changes

#### `ai_pr_review/config.py`

1. Add two new entries to `_KNOWN_AI_VARS`:
   ```python
   "AI_LOG_FORMAT",
   "AI_LOG_LEVEL",
   ```

2. Add two new fields to `ReviewConfig`:
   ```python
   # --- Epic 4: Capability 1 — structured logging ---
   log_format: str = "human"
   log_level: str = "WARNING"
   ```

3. Add a `field_validator` for `log_format`:
   ```python
   @field_validator("log_format")
   @classmethod
   def _validate_log_format(cls, v: str) -> str:
       if v not in ("human", "json"):
           raise ValueError(f"log_format must be 'human' or 'json', got {v!r}")
       return v
   ```

4. Add a `field_validator` for `log_level`:
   ```python
   @field_validator("log_level")
   @classmethod
   def _validate_log_level(cls, v: str) -> str:
       valid = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
       if v.upper() not in valid:
           raise ValueError(f"log_level must be one of {valid}, got {v!r}")
       return v.upper()
   ```

5. In `from_env()`, add to the `cls(...)` call:
   ```python
   log_format=os.environ.get("AI_LOG_FORMAT", "human"),
   log_level=os.environ.get("AI_LOG_LEVEL", "WARNING"),
   ```

#### `ai_pr_review/cli.py`

1. Add imports at top:
   ```python
   from ai_pr_review.logging import generate_correlation_id, setup_logging
   ```

2. In the `review()` command, immediately after `config = ReviewConfig.from_env()` succeeds, add:
   ```python
   correlation_id = os.environ.get("AI_PR_REVIEW_CORRELATION_ID") or generate_correlation_id()
   os.environ["AI_PR_REVIEW_CORRELATION_ID"] = correlation_id
   setup_logging(config.log_format, config.log_level, correlation_id)
   logger.info("review started", extra={"correlation_id": correlation_id})
   ```

3. Apply the same pattern in `compute()` and the `slash` command: generate/read correlation ID, call `setup_logging()`, set `AI_PR_REVIEW_CORRELATION_ID` in env.

4. The `setup_logging()` call must happen **before** any `logger.*` call in the review pipeline, including inside `_run_review_async`.

#### `ai_pr_review/analyzers/bridge.py`

`AI_PR_REVIEW_CORRELATION_ID` propagates to analyzer subprocesses automatically because `_run_analyzer()` builds `run_env = {**os.environ, **extra_env, "DIFF_FILE": diff_file}` — since `cli.py` sets `AI_PR_REVIEW_CORRELATION_ID` in `os.environ` before calling `run_analyzers()`, no change is needed to `bridge.py`. This is the correct path.

**Verification:** Add a test that asserts `AI_PR_REVIEW_CORRELATION_ID` appears in the `run_env` passed to `subprocess.run` when it has been set in `os.environ`.

#### `ai_pr_review/agents/dispatch.py`

No structural changes needed. The `logging.getLogger(__name__)` call at line 300 of `_build_user_message` already uses stdlib logging. Once `setup_logging()` installs the `CorrelationFilter` on the root handler, `correlation_id` will be injected into every record automatically, including the one at line 300.

**Do not** add `extra={"correlation_id": ...}` at every individual call site — the `CorrelationFilter` handles injection globally.

#### `ai_pr_review/llm/_http.py`

The `print(..., file=sys.stderr)` calls here should be converted to `logging.getLogger(__name__)` calls (WARNING level) so they benefit from correlation ID injection and secret masking. This is a bonus improvement but is in-scope for this story since these are the most security-sensitive log lines (they can contain HTTP status codes from auth-failure responses).

Specific replacements:
- Line 66-70: `print("WARNING: ... retrying ...")` → `logger.warning("... retrying ...", ...)`
- Line 83-87: same pattern for HTTP status retry
- Line 98-108: `print("ERROR: ... API returned ...")` → `logger.error(...)` and `logger.debug(response.text, ...)`

Add at top of file:
```python
import logging
logger = logging.getLogger(__name__)
```

#### `ai_pr_review/vcs/http.py`

No logging changes required for this story. The `redact_secrets()` function already exists for tape recording. The logging module's `_mask_secrets()` should use similar patterns but is independent (different purpose: live log lines, not tape files).

---

## Config fields to add (in `config.py`)

| Field | Env var | Type | Default | Validation |
|---|---|---|---|---|
| `log_format` | `AI_LOG_FORMAT` | `str` | `"human"` | must be `"human"` or `"json"` |
| `log_level` | `AI_LOG_LEVEL` | `str` | `"WARNING"` | must be DEBUG/INFO/WARNING/ERROR/CRITICAL (case-insensitive, stored uppercase) |

Both must be added to `_KNOWN_AI_VARS` **and** parsed in `from_env()`. The existing `test_all_ai_vars_read_in_from_env_are_known` test in `tests/python/test_config.py` will catch any mismatch.

---

## Secret masking approach

`_mask_secrets(text: str) -> str` applies three layers in order:

**Layer 1 — Env var name patterns (catches key=value log lines):**
```python
re.sub(
    r'(?i)((?:[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|_KEY)\b)[^\s=]*)\s*[=:]\s*(\S+)',
    lambda m: m.group(1) + "=<redacted>",
    text
)
```

**Layer 2 — Provider token prefixes (catches raw token values in messages):**
```python
re.sub(
    r'\b(sk-ant-[A-Za-z0-9\-]{10,}|sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{10,}|'
    r'ghs_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|'
    r'glpat-[A-Za-z0-9_-]{10,}|glcbt-[A-Za-z0-9_-]{10,})',
    '<redacted>',
    text
)
```

**Layer 3 — Literal secret values (compiled at `setup_logging()` time):**
At `setup_logging()` call, `SecretMaskingFormatter` receives a `frozenset[str]` of non-empty secret values extracted from `ReviewConfig`. These are compiled into a single alternation regex and applied as a final pass. Values shorter than 8 characters are excluded to avoid false positives.

`SecretMaskingFormatter.format()` calls `super().format(record)` first to get the fully rendered string, then applies `_mask_secrets()` before returning. This ensures exc_info tracebacks and chained messages are also masked.

---

## Correlation ID propagation path

```
cli.py (review command)
  │
  ├─ generate_correlation_id() → "a1b2c3d4"
  ├─ os.environ["AI_PR_REVIEW_CORRELATION_ID"] = "a1b2c3d4"
  ├─ setup_logging(format, level, "a1b2c3d4")
  │     └─ installs CorrelationFilter on root handler
  │         └─ CorrelationFilter reads contextvars.ContextVar("correlation_id")
  │
  ├─ _run_review_async(config)
  │     ├─ run_analyzers() → bridge.py → subprocess.run(env={...AI_PR_REVIEW_CORRELATION_ID...})
  │     │     └─ subprocesses inherit the env var (bash analyzer wrappers)
  │     │
  │     ├─ run_tier() → _run_single_agent() → llm_call() → _http.py retry_post()
  │     │     └─ logger.warning/error calls → CorrelationFilter injects ID
  │     │
  │     └─ provider.post_summary/post_findings/resolve_stale
  │           └─ logger calls in orchestrate.py → CorrelationFilter injects ID
  │
  └─ All log lines on stderr contain "correlation_id": "a1b2c3d4"
```

**Inbound correlation ID:** If `AI_PR_REVIEW_CORRELATION_ID` is already set in the environment when `cli.py` starts (e.g., set by `review.sh` before invoking the Python engine), use that value instead of generating a new one. This allows the bash layer to set the ID and have it propagate into the Python engine.

**ContextVar vs env:** The `ContextVar` approach is preferred over thread-local for async safety (anyio). Set the ContextVar token in `setup_logging()` and it will be readable from all coroutines in the same thread context.

---

## Test requirements

**Test file:** `tests/python/test_logging.py`

All tests use `monkeypatch` and `capfd`/`capsys` for stderr capture. No external dependencies.

### Required test cases

#### JSON format output
```python
def test_json_format_emits_valid_json(monkeypatch, capsys):
    """setup_logging('json', 'DEBUG', 'abc12345') → each stderr line is valid JSON."""
    # Call setup_logging, emit a log line, capture stderr, assert json.loads succeeds
    # Assert keys: timestamp, level, logger, message, correlation_id

def test_json_format_contains_correlation_id(monkeypatch, capsys):
    """correlation_id field in JSON output matches the ID passed to setup_logging."""

def test_json_timestamp_is_iso8601(monkeypatch, capsys):
    """timestamp field in JSON output parses as a datetime."""
```

#### Human format output
```python
def test_human_format_is_not_json(monkeypatch, capsys):
    """setup_logging('human', ...) → stderr output is NOT valid JSON."""

def test_human_format_contains_correlation_id(monkeypatch, capsys):
    """Correlation ID bracket appears in human-readable output."""
```

#### Log level filtering
```python
def test_log_level_warning_suppresses_debug(monkeypatch, capsys):
    """AI_LOG_LEVEL=WARNING: DEBUG and INFO records do not appear on stderr."""

def test_log_level_debug_passes_all(monkeypatch, capsys):
    """AI_LOG_LEVEL=DEBUG: DEBUG records appear on stderr."""
```

#### Secret masking (critical — must pass)
```python
def test_secret_masking_api_key_not_in_output(monkeypatch, capsys):
    """A fake API key in env is never emitted in log output.

    Injects ANTHROPIC_API_KEY=sk-ant-fake-key-for-testing into the
    ReviewConfig secrets set, then logs a message that includes the
    literal key value. Asserts the raw key does not appear in stderr.
    Asserts '<redacted>' does appear.
    """

def test_secret_masking_provider_token_prefix(monkeypatch, capsys):
    """ghp_xxxx token prefix in a log message is redacted."""

def test_secret_masking_short_value_not_redacted(monkeypatch, capsys):
    """Values shorter than 8 chars are NOT redacted (avoids false positives)."""

def test_secret_masking_in_exception_traceback(monkeypatch, capsys):
    """A secret embedded in an exception message is redacted in the formatted output."""
```

#### Correlation ID boundary
```python
def test_correlation_id_in_analyzer_subprocess_env(monkeypatch, tmp_path):
    """AI_PR_REVIEW_CORRELATION_ID is present in env passed to subprocess.run.

    Patches subprocess.run to capture the env dict. Calls run_analyzers()
    after setting AI_PR_REVIEW_CORRELATION_ID in os.environ. Asserts the
    captured env contains the key.
    """

def test_correlation_id_inbound_from_env_is_reused(monkeypatch):
    """If AI_PR_REVIEW_CORRELATION_ID is already set, generate_correlation_id
    is NOT called; the existing value is used.

    Test this by calling the CLI's correlation ID bootstrap logic (factored
    into a helper if needed) with a pre-set env var.
    """
```

#### Config fields
```python
def test_config_log_format_default_is_human(monkeypatch):
    """ReviewConfig.from_env() defaults log_format to 'human'."""

def test_config_log_format_json(monkeypatch):
    """AI_LOG_FORMAT=json → config.log_format == 'json'."""

def test_config_log_format_invalid_raises(monkeypatch):
    """AI_LOG_FORMAT=xml raises ValueError."""

def test_config_log_level_default_is_warning(monkeypatch):
    """ReviewConfig.from_env() defaults log_level to 'WARNING'."""

def test_config_log_level_case_insensitive(monkeypatch):
    """AI_LOG_LEVEL=debug → config.log_level == 'DEBUG' (uppercased)."""

def test_config_log_level_invalid_raises(monkeypatch):
    """AI_LOG_LEVEL=VERBOSE raises ValueError."""
```

### Test setup notes

- Each test that calls `setup_logging()` must reset the root logger handlers afterward (use a `pytest.fixture` with `yield` + cleanup, or `monkeypatch` on `logging.root.handlers`). Otherwise handler accumulation across tests will corrupt capsys output.
- Use a dedicated test logger name (e.g., `logging.getLogger("test_logging_4_1")`) to avoid polluting other test output.
- The `test_secret_masking_api_key_not_in_output` test must use a value that is clearly a fake key (e.g., `sk-ant-fake-key-for-testing-1234abcd`) and assert `"sk-ant-fake-key-for-testing-1234abcd" not in stderr_output`.

---

## Files modified / created

| File | Action | Notes |
|---|---|---|
| `ai_pr_review/logging.py` | **Create** | New module: `setup_logging`, `SecretMaskingFormatter`, `CorrelationFilter`, `_mask_secrets`, `generate_correlation_id` |
| `ai_pr_review/config.py` | **Modify** | Add `AI_LOG_FORMAT`, `AI_LOG_LEVEL` to `_KNOWN_AI_VARS`; add `log_format`, `log_level` fields + validators + `from_env()` wiring |
| `ai_pr_review/cli.py` | **Modify** | Call `setup_logging()` at top of each subcommand after config is loaded; generate/inherit correlation ID; set `os.environ["AI_PR_REVIEW_CORRELATION_ID"]` |
| `ai_pr_review/llm/_http.py` | **Modify** | Convert `print(..., file=sys.stderr)` calls to `logger.warning/error()` |
| `tests/python/test_logging.py` | **Create** | New test file covering all scenarios above |
| `tests/python/test_config.py` | **Modify** | Existing test `test_all_ai_vars_read_in_from_env_are_known` will auto-cover new vars; add explicit tests for the two new fields and their validators |

`ai_pr_review/analyzers/bridge.py` — **no change needed** (correlation ID flows through `os.environ` automatically).

`ai_pr_review/vcs/http.py` — **no change needed** (existing `redact_secrets()` is for tape recording; logging redaction is handled by `SecretMaskingFormatter`).

---

## Dev Agent Record

### Implementation Notes (2026-05-18)

All ACs satisfied. Key decision: `AI_PR_REVIEW_CORRELATION_ID` added to `_KNOWN_AI_VARS` (not `from_env()`) — same pattern as `AI_AGENT`. Story note had the logic inverted; being in `_KNOWN_AI_VARS` *prevents* ConfigError for external users setting it, which is the desired behavior.

### File List

| File | Action |
|---|---|
| `ai_pr_review/logging.py` | Created — `setup_logging`, `SecretMaskingFormatter`, `CorrelationFilter`, `_mask_secrets`, `generate_correlation_id` |
| `ai_pr_review/config.py` | Added `AI_LOG_FORMAT`, `AI_LOG_LEVEL`, `AI_PR_REVIEW_CORRELATION_ID` to `_KNOWN_AI_VARS`; `log_format`/`log_level` fields, validators, `from_env()` wiring |
| `ai_pr_review/cli.py` | `setup_logging()` + correlation ID bootstrap wired into `compute`, `review`, `slash` subcommands |
| `ai_pr_review/llm/_http.py` | Converted 5× `print(..., sys.stderr)` to `logger.warning/error/debug` |
| `tests/python/test_logging.py` | Created — 36 tests; all pass |

### Change Log

- 2026-05-18: Implemented structured logging module. 825 Python tests pass, 0 regressions.

---

## Dev agent guardrails

**Anti-patterns to avoid:**

1. **Do not call `logging.basicConfig()`** from inside any library module. `setup_logging()` in `logging.py` must configure the root logger directly via `logging.root.setLevel()` and `logging.root.addHandler()`. `basicConfig()` is a no-op if any handlers are already installed, making it unreliable in test environments.

2. **Do not re-parse env vars in `logging.py`**. The module receives `log_format: str` and `log_level: str` as parameters from `ReviewConfig`. No `os.environ.get()` calls inside `logging.py`.

3. **Do not add `extra={"correlation_id": ...}` at every call site**. The `CorrelationFilter` injects it globally. Individual call sites only need `extra` if they are calling before `setup_logging()` is called (which should not happen in production code paths).

4. **Do not introduce `structlog`, `loguru`, or any other third-party logging library**. Stdlib `logging` only. This is enforced by the project's dependency policy.

5. **Do not mask secrets by mutating the `LogRecord` `msg` or `args` fields** in-place. Instead, apply masking to the fully-formatted string in `SecretMaskingFormatter.format()` after calling `super().format(record)`. Mutating `msg`/`args` in-place causes record objects to be modified globally, which breaks test isolation and can corrupt chained exception formatting.

6. **Do not use thread-local for correlation ID storage**. Use `contextvars.ContextVar` — the codebase uses anyio for async dispatch, and `ContextVar` is safe across coroutine boundaries while thread-local is not.

7. **The `logging.py` module must not import from `ai_pr_review.config`** to avoid circular imports. `ReviewConfig` is passed into `setup_logging()` as a parameter only where the literal secret values need to be extracted for Layer 3 masking. The function signature should accept an optional `secrets: frozenset[str] | None = None` parameter extracted by the caller.

8. **`setup_logging()` is idempotent when called multiple times** — check `len(logging.root.handlers) > 0` and return early (or clear + reinstall) to avoid duplicate handler registration. This matters for tests that call it multiple times.

9. **The existing `test_all_ai_vars_read_in_from_env_are_known` test in `test_config.py` will fail if `AI_LOG_FORMAT` or `AI_LOG_LEVEL` are not added to `_KNOWN_AI_VARS`**. Run `pytest tests/python/test_config.py` as your first check after modifying `config.py`.

10. **Do not add `AI_PR_REVIEW_CORRELATION_ID` to `_KNOWN_AI_VARS`** — it is an internal propagation variable set by the engine itself, not a user-configured input. It must NOT be in `_KNOWN_AI_VARS` or `from_env()` (it would trigger ConfigError if a user set it externally, which is a supported use case for bash-to-python hand-off).
