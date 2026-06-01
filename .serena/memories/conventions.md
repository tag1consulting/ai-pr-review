# Conventions

## Python
- **Pydantic v2** for all data models; `model_validator(mode="after")`, `field_validator` v2 API only.
- **Strict typing** — mypy strict; every function annotated; `from __future__ import annotations` where needed for forward refs.
- **No bare `except`** — always catch specific exceptions.
- **async-first** — I/O functions are async (httpx, anyio); sync wrappers only at CLI boundary.
- **Module-level loggers** — `logger = logging.getLogger(__name__)` in every module that logs.
- **`_` prefix** — private module-level symbols; `__` for truly private class internals.
- **Pydantic config** — `model_config = ConfigDict(frozen=True)` for immutable value objects.
- Standard library `logging`; no third-party log libs.

## Bash
- POSIX-compatible bash; shellcheck-clean.
- Functions named `snake_case`; globals `ALL_CAPS`.
- Each `analyzers/run-*.sh` reads `*_MOCK_FILE` env var for test isolation (never set in production).
- `vcs/common.sh` sourced by all post-review scripts; do not duplicate its helpers.

## Findings JSON schema
All agents and analyzers emit a `json-findings` fenced block:
```json
[{"severity": "high|medium|low|info", "file": "...", "line": N, "title": "...", "body": "..."}]
```
`file` and `line` may be null for body-level findings. `Finding` Pydantic model lives in `ai_pr_review/findings/models.py`.

## Language profile files
- Location: `language-profiles/<lowercase-key>.md`
- Filename must match the key returned by `detect_language()` in `lib/languages.sh`.
- Any new language profile requires a parallel entry in `lib/languages.sh` AND `ai_pr_review/languages.py:_EXT_MAP`.

## Naming patterns
- Agent prompt files: `prompts/<agent-name>.md`
- Static analyzer wrappers: `analyzers/run-<tool>.sh`
- Python subpackage per concern: `agents/`, `findings/`, `llm/`, `vcs/`, `slash/`, `diff/`, `context/`, `review/`
