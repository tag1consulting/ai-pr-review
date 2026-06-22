# Contributing to AI PR Review

Quick recipes for the most common contribution types. For deep implementation details, see [docs/architecture-internals.md](docs/architecture-internals.md). For the compact AI-agent reference, see [CLAUDE.md](CLAUDE.md).

## Local setup

```bash
# Prerequisites: Python 3.11+, uv or pip
git clone git@github.com:tag1consulting/ai-pr-review.git
cd ai-pr-review
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # installs pytest, ruff, mypy, and runtime deps

# Run the test suite
pytest tests/python/ -q

# Lint and type-check
ruff check ai_pr_review/
mypy ai_pr_review/
```

## Adding a static analyzer

Static analyzers live in `ai_pr_review/analyzers/native/`.

### 1. Create the native Python analyzer

Create `ai_pr_review/analyzers/native/yourtool.py`. Follow the pattern of an existing simple analyzer (e.g. `hadolint.py` or `ruff.py`):

```python
from __future__ import annotations
import subprocess
from pathlib import Path
from ai_pr_review.models import Finding

def run(changed_files: list[str], workspace: str) -> list[Finding]:
    matching = [f for f in changed_files if f.endswith((".ext1", ".ext2"))]
    if not matching:
        return []
    result = subprocess.run(
        ["yourtool", "--json"] + matching,
        capture_output=True, text=True, cwd=workspace,
    )
    findings = []
    for item in _parse(result.stdout):
        findings.append(Finding(
            severity=_severity(item),
            confidence=90,
            file=item["file"],
            line=item.get("line"),
            finding=item["message"],
            source="yourtool",
        ))
    return findings
```

Key rules:
- Accept `changed_files: list[str]` and `workspace: str`; return `list[Finding]`
- Filter to relevant file extensions early
- Return `[]` and log a warning if the binary is missing (`shutil.which`)
- Hard-code the `source` field to your tool name
- Match the severity mapping used by existing analyzers: `"Critical"` (90 confidence) → `"High"` → `"Medium"` → `"Low"` (50 confidence), following the patterns in `ai_pr_review/analyzers/native/`

### 2. Register in the bridge

In `ai_pr_review/analyzers/bridge.py`, import your function and add it to the `_ANALYZERS` table:

```python
from ai_pr_review.analyzers.native import yourtool as _yourtool
# ...
_ANALYZERS = {
    # ... existing entries ...
    "yourtool": _yourtool.run,
}
```

### 3. Add Python tests

Create `tests/python/test_analyzer_yourtool.py` with fixture-based tests. See `tests/python/test_analyzer_shellcheck.py` for the pattern.

### 4. Update documentation

- Add a row to the analyzer table in `CLAUDE.md` (mock env var table)
- Add a row to the analyzer table in `README.md` and `docs/static-analyzers.md`
- If bundled in the container, add the install step to `Dockerfile`

## Adding an agent

### 1. Create the prompt

Create `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` fenced code block. Look at existing prompts for the pattern.

### 2. Register in the agent roster

Add an `AgentSpec` entry to `ai_pr_review/agents/roster.py` with the agent name, prompt path, tier (1 or 2 — controls parallel dispatch group), `max_output_tokens`, `full_mode_only` flag, `conditional_trigger` (file-pattern or `None`), and `context_enrichment_eligible` flag.

### 3. Add conditional gate logic (if needed)

If the agent should only run when specific files change, set `conditional_trigger` to a glob/regex pattern; the gate evaluation lives in `ai_pr_review/agents/gates.py`.

### 4. Add unit tests

Add unit-test coverage in `tests/python/agents/` for any custom gate logic.

### 5. Enable suggestions (optional)

If your agent produces concrete line-level fixes, ensure the prompt includes the suggestion addendum instructions or reference `prompts/suggestion-addendum.md` from the agent's prompt.

## Adding a language profile

1. Create `language-profiles/<language>.md` — the filename (without `.md`) must match the lowercase language key returned by `detect_language()` in `ai_pr_review/languages.py`.
2. Register the new extension(s) in `ai_pr_review/languages.py:_EXT_MAP` if they are not already mapped.
3. The file content is injected verbatim into the agent prompt context when that language is detected in the diff.
4. See [CLAUDE.md](CLAUDE.md#adding-a-language-profile) for the full extension-to-language mapping.

## Adding a VCS provider

This is a larger contribution. The pattern:

1. Create `ai_pr_review/vcs/<provider>.py` following the structure of `ai_pr_review/vcs/bitbucket.py` or `ai_pr_review/vcs/gitlab.py`.
2. Implement the `VcsProvider` protocol defined in `ai_pr_review/vcs/protocol.py`: `post_summary`, `post_findings`, `advance_sha_watermark`, `resolve_stale`, and `post_skip_comment`.
3. Register the new provider in `ai_pr_review/vcs/__init__.py` and wire it into `ai_pr_review/cli.py`.
4. Add tests in `tests/python/vcs/test_<provider>.py`.
5. Add a setup guide in `docs/<provider>-setup.md`.

See [docs/architecture-internals.md](docs/architecture-internals.md#multi-provider-support-github--bitbucket-cloud--gitlab) for how the provider abstraction works.

## Pre-PR checklist

Before opening a pull request:

- [ ] `pytest tests/python/ -q` — all tests pass
- [ ] `ruff check ai_pr_review/` and `mypy ai_pr_review/` — no new lint or type errors
- [ ] Update `CLAUDE.md` if you changed interfaces (new env vars, changed function signatures)
- [ ] If you added an `AI_*` env var, register it in `_KNOWN_AI_VARS` in `ai_pr_review/config.py` and add a `from_env()` field — otherwise the engine raises `ConfigError` at startup
- [ ] Update `README.md` and `docs/` pages if you changed user-facing behavior
- [ ] Run `/comprehensive-review --quick` to catch issues before the CI review

## Code style

- Python code follows PEP 8; ruff enforces E, F, W, I, UP, B, and SIM rule sets
- Native analyzer modules use the mock env var pattern for testing — never call real binaries in tests
- Findings JSON uses the schema documented in [docs/architecture-internals.md](docs/architecture-internals.md#agent-output-schema)
