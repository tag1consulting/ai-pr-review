# Contributing to AI PR Review

Quick recipes for the most common contribution types. For deep implementation details, see [docs/architecture-internals.md](docs/architecture-internals.md). For the compact AI-agent reference, see [CLAUDE.md](CLAUDE.md).

## Local setup

```bash
# Prerequisites: bash 4+, jq, bats-core, shellcheck
git clone git@github.com:tag1consulting/ai-pr-review.git
cd ai-pr-review

# Run the full test suite
bats tests/*.bats

# Lint shell scripts
shellcheck review.sh llm-call.sh post-review.sh post-review-bitbucket.sh \
  post-review-gitlab.sh analyzers/run-shellcheck.sh analyzers/run-cve-check.sh
```

### Python engine setup

```bash
# Prerequisites: Python 3.11+, uv or pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # installs pytest, ruff, mypy, and runtime deps

# Run the Python test suite
pytest tests/python/ -q

# Lint and type-check
ruff check ai_pr_review/
mypy ai_pr_review/
```

## Adding a static analyzer

Static analyzers live in `ai_pr_review/analyzers/native/` (Python engine, default) and `analyzers/run-<tool>.sh` (deprecated bash engine). The Python path is the canonical one; the bash wrapper is only needed to keep the deprecated engine working.

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
- Match the severity mapping documented in `docs/analyzers-bash-inventory.md`

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

### 4. Create the bash wrapper (bash engine parity — optional for new analyzers)

The bash engine is deprecated and will be removed in a future major release (targeted for v2.0). New analyzers added after v1.4.0 may omit the bash wrapper entirely; existing wrappers are frozen. If you are backporting an analyzer to the bash engine for compatibility reasons, create `analyzers/run-<tool>.sh` following the existing wrapper pattern — it must produce the same findings schema and source tag as the Python analyzer.

### 5. Wire it into review.sh (bash engine — optional, same caveat as step 4)

In `review.sh`, add the analyzer call to both the parallel and sequential blocks alongside the existing ones. Skip this step if omitting the bash wrapper.

### 6. Update documentation

- Add a row to the analyzer table in `CLAUDE.md` (mock env var table)
- Add a row to the analyzer table in `README.md` and `docs/static-analyzers.md`
- Add a row to `docs/analyzers-bash-inventory.md`
- If bundled in the container, add the install step to `Dockerfile`

## Adding an agent

### 1. Create the prompt

Create `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` fenced code block. Look at existing prompts for the pattern.

### 2. Wire it into review.sh

```bash
# Choose the context message variant:
#   CODE_CONTEXT_MSG  — most agents (code-focused, no commit log)
#   FULL_CONTEXT_MSG  — agents that need project context (pr-summarizer, architecture-reviewer)
#   BLIND_MSG         — zero-context agents (blind-hunter only)

call_agent "your-agent" "$AI_MODEL_STANDARD" \
  "${SCRIPT_DIR}/prompts/your-agent.md" \
  "$CODE_CONTEXT_MSG" "$YOUR_AGENT_OUTPUT" "$AI_MAX_TOKENS_PER_AGENT"
AGENT_OUTPUTS+=("$YOUR_AGENT_OUTPUT")
```

### 3. Add to parallel execution

Place the agent into a tier in the parallel code path. Tier 1 runs on every review; Tier 2 runs only in full mode:

```bash
# In the parallel block:
TIER1_OUTPUTS+=("$YOUR_AGENT_OUTPUT")   # or TIER2_OUTPUTS
call_agent_bg "your-agent" "$AI_MODEL_STANDARD" \
  "${SCRIPT_DIR}/prompts/your-agent.md" \
  "$CODE_CONTEXT_MSG" "$YOUR_AGENT_OUTPUT" "$AI_MAX_TOKENS_PER_AGENT" &
```

Also add the sequential `call_agent` in the `else` branch.

### 4. Enable suggestions (optional)

If your agent produces concrete line-level fixes, add your agent name to the `agents_with_suggestion_addendum` pattern string inside `effective_prompt()` in `lib/agents.sh` so it appends `prompts/suggestion-addendum.md` to its system prompt.

## Adding a language profile

1. Create `language-profiles/<language>.md` — the filename (without `.md`) must match the lowercase language key from `detect_language()` in `lib/languages.sh`.
2. The file content is injected verbatim into `FULL_CONTEXT_MSG` and `CODE_CONTEXT_MSG` when that language is detected in the diff.
3. See [CLAUDE.md](CLAUDE.md#adding-a-language-profile) for the full extension-to-language mapping.

## Adding a VCS provider

This is a larger contribution. The pattern:

1. Create `post-review-<provider>.sh` following the structure of `post-review-bitbucket.sh` or `post-review-gitlab.sh`.
2. Source `vcs/common.sh` for shared helpers (`severity_icon`, `format_source_tag`, `classify_risk`, `format_body_finding`, `build_agent_prompt`, `parse_valid_lines`, `parse_diff_new_lines`, `mktemp_tracked`, `cleanup`).
3. Implement the provider-specific functions: `truncate_body`, `find_existing_summary_id`, `build_comment_body`, `post_summary_with_findings`, `update_sha_marker`, `_cleanup_duplicate_summary_comments`.
4. Add a `VCS_PROVIDER` case in `review.sh` to select your script.
5. Add tests in `tests/post_review_<provider>_functions.bats`.
6. Add a setup guide in `docs/<provider>-setup.md`.

See [docs/architecture-internals.md](docs/architecture-internals.md#multi-provider-support-github--bitbucket-cloud--gitlab) for how the provider abstraction works.

## Pre-PR checklist

Before opening a pull request:

- [ ] `bats tests/*.bats` — all tests pass
- [ ] `shellcheck` on any modified `.sh` files
- [ ] `pytest tests/python/ -q` — Python tests pass (if you touched `ai_pr_review/`)
- [ ] `ruff check ai_pr_review/` and `mypy ai_pr_review/` — no new lint or type errors
- [ ] Update `CLAUDE.md` if you changed interfaces (new env vars, new scripts, changed function signatures)
- [ ] If you added an `AI_*` env var, register it in `_KNOWN_AI_VARS` in `ai_pr_review/config.py` and add a `from_env()` field — otherwise the Python engine raises `ConfigError` at startup
- [ ] Update `README.md` and `docs/` pages if you changed user-facing behavior
- [ ] Run `/comprehensive-review --quick` to catch issues before the CI review

## Code style

- Shell scripts use `set -euo pipefail`
- Functions are tested via `load_function` extraction in bats (see [docs/architecture-internals.md](docs/architecture-internals.md#test-architecture))
- Static analyzer scripts use the mock env var pattern for testing — never call real binaries in tests
- Findings JSON uses the schema documented in [docs/architecture-internals.md](docs/architecture-internals.md#agent-output-schema)
