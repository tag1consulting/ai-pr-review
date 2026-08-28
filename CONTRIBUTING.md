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

## Security canary tests

`tests/python/test_security_canary.py` (marked `@pytest.mark.security`) enforces the invariant that the action never executes any file from the checked-out workspace. See `SECURITY.md` for the full invariant statement.

The fixture in `tests/security/canary-workspace/` contains six tripwire files (`Makefile`, `setup.py`, `package.json`, `.pre-commit-config.yaml`, `conftest.py`, `.semgrep.yml`) designed to write sentinel files to `$CANARY_DIR` if the action ever executes them rather than reading them as data.

**A failure in a `@pytest.mark.security` test is a security regression, not a flaky test.** Do not skip it, xfail it, or delete it. Instead:

1. Identify which code path triggered the canary sentinel (the assertion message names the sentinels that fired).
2. Report the regression privately via [GitHub's private vulnerability reporting](https://github.com/tag1consulting/ai-pr-review/security/advisories/new) or email security@tag1consulting.com.
3. Do not merge the offending PR until the vulnerability is remediated.

## Adding a static analyzer

Static analyzers live in `ai_pr_review/analyzers/native/`.

### 1. Create the native Python analyzer

Create `ai_pr_review/analyzers/native/yourtool.py`. Follow the pattern of an existing simple analyzer (e.g. `hadolint.py` or `ruff.py`):

```python
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "yourtool"


def _run_yourtool(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    matching = [f for f in changed_files.source if Path(f).is_file() and f.endswith((".ext1", ".ext2"))]
    if not matching:
        return []

    if not shutil.which("yourtool"):
        logger.warning("[ai-pr-review] WARNING: yourtool not found; skipping.")
        return []

    result = subprocess.run(
        ["yourtool", "--json", *matching],
        capture_output=True, text=True, cwd=".", timeout=120,
    )
    findings: list[Finding] = []
    for item in _parse(result.stdout):
        findings.append(Finding(
            severity=_severity(item),
            confidence=_CONFIDENCE,
            file=item["file"],
            line=item.get("line"),
            finding=item["message"],
            source=_SOURCE,
            category="lint",
        ))
    return findings
```

Key rules (see any file in `ai_pr_review/analyzers/native/` for the full pattern, e.g. `ruff.py`):
- Signature is exactly `(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` — this is the `NativeAnalyzerFn` type alias in `bridge.py`. `ChangedFiles` (from `ai_pr_review/manifest.py`) is the pre-categorized, typed set of changed files; filter to the relevant typed list (`changed_files.python`, `changed_files.go`, `changed_files.source`, etc.) rather than re-deriving file types yourself.
- `Finding` comes from `ai_pr_review.findings.models`, not `ai_pr_review.models` (no such module exists).
- Return `[]` and log a `logger.warning("[ai-pr-review] WARNING: ...")` if the binary is missing (`shutil.which`), on a `subprocess.TimeoutExpired`, on an `OSError`, or on any other failure to run — every guard rail fails soft, never raises.
- Wrap each `Finding(...)` construction in its own `try/except (ValueError, TypeError)` so one malformed item doesn't drop the whole run.
- Hard-code the `source` field to your tool name as a module-level `_SOURCE` constant.
- Match the severity mapping used by existing analyzers: `"Critical"` → `"High"` → `"Medium"` → `"Low"`, with a module-level `_CONFIDENCE` constant (see the table in `docs/static-analyzers.md` for the range other analyzers use, roughly 80-95 by tool maturity).

### 2. Register in the bridge

In `ai_pr_review/analyzers/bridge.py`, import your function and add an `AnalyzerSpec` entry to the `_ANALYZERS` list:

```python
from ai_pr_review.analyzers.native.yourtool import _run_yourtool
# ...
_ANALYZERS: list[AnalyzerSpec] = [
    # ... existing entries ...
    AnalyzerSpec("yourtool", ["source"], _run_yourtool),
]
```

`AnalyzerSpec`'s second field is `required_file_types`: a list of `ChangedFiles` attribute names (e.g. `["python"]`, `["go"]`) that gates eligibility — the analyzer only runs when at least one of those lists is non-empty. Pass `[]` to always run (like `semgrep`/`trufflehog`/`docs-drift-check`). `ANALYZER_NAMES` (used by the `analyzers`/`exclude-analyzers` allowlist validation) derives automatically from this list — no separate registration needed there.

**Also add your analyzer's name to `_ANALYZER_PREFIXES` in `ai_pr_review/findings/scope.py`.** This is a second, easy-to-miss registration site: without it, your analyzer's findings are never diff-scoped, never rolled up, and never counted as corroborating an LLM agent's finding on the same issue.

### 3. Add Python tests

Create `tests/python/test_analyzer_yourtool.py` with fixture-based tests. See `tests/python/test_analyzer_ruff.py` for the pattern: a `_make_cf()` helper building a `ChangedFiles` instance, module-qualified `patch("ai_pr_review.analyzers.native.yourtool.shutil.which", ...)` and `.subprocess.run` mocks (never invoke the real binary in unit tests), guard-rail tests (binary absent, timeout, bad exit code, malformed output) grouped separately from finding-content tests, and bridge-integration tests at the tail confirming `run_analyzers` dispatches to your `native_fn` and skips it when no eligible files are present.

### 4. Update documentation

- Add a row to the analyzer table in `docs/static-analyzers.md` and the analyzer-count/module map in `CLAUDE.md`
- Add your analyzer's name to both valid-name lists in `action.yml` (`analyzers` and `exclude-analyzers` input descriptions)
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

### 5. Wire up governance

Add the agent name to `_AGENTS_WITH_FINDINGS_TRAILER` in `ai_pr_review/agents/dispatch.py`. This frozenset is what actually injects `prompts/_governance.md` (the Three Laws and the five governance rules), `prompts/_knowledge-cutoff.md`, and `prompts/_trailer-findings.md` into the agent's prompt. An agent omitted from it runs with no governance, no version-hallucination guard, and no findings-schema instruction, and will silently emit unparseable output.

### 6. Enable suggestions (optional)

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
