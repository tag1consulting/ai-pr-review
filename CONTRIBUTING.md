# Contributing to AI PR Review

Quick recipes for the most common contribution types. For deep implementation details, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). For the compact AI-agent reference, see [CLAUDE.md](CLAUDE.md).

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

Static analyzers are the easiest contribution — self-contained scripts with a consistent pattern.

### 1. Create the wrapper script

Create `analyzers/run-<tool>.sh`. Every analyzer follows the same contract:

```bash
#!/usr/bin/env bash
set -euo pipefail

CHANGED_FILES="$1"
# Filter to files your tool cares about
MATCHING_FILES=$(echo "$CHANGED_FILES" | grep -E '\.(ext1|ext2)$' || true)
if [[ -z "$MATCHING_FILES" ]]; then
  echo "[]"
  exit 0
fi

# Support mock file for testing
if [[ -n "${YOURTOOL_MOCK_FILE:-}" ]]; then
  cat "$YOURTOOL_MOCK_FILE"
  exit 0
fi

# Check if binary is installed
if ! command -v yourtool &>/dev/null; then
  echo "WARNING: yourtool not found, skipping" >&2
  echo "[]"
  exit 0
fi

# Run the tool and transform output to findings JSON
yourtool --json $MATCHING_FILES | jq '[.[] | {
  severity: (if .level == "error" then "High" elif .level == "warning" then "Medium" else "Low" end),
  confidence: 90,
  file: .file,
  line: .line,
  finding: .message,
  source: "yourtool"
}]'
```

Key rules:
- Accept `$CHANGED_FILES` (newline-separated) as the first argument
- Filter to relevant file extensions early
- Output a JSON array on stdout matching the findings schema
- Support a `<TOOL>_MOCK_FILE` env var for testing
- Return `[]` if the binary is missing (emit WARNING to stderr)
- Hard-code the `source` field to your tool name

### 2. Add test fixtures

Create `tests/fixtures/yourtool/` with sample tool output files. Create `tests/run_yourtool.bats`:

```bash
#!/usr/bin/env bats

setup() {
  export YOURTOOL_MOCK_FILE="$BATS_TEST_DIRNAME/fixtures/yourtool/sample-output.json"
}

@test "yourtool: produces findings from sample output" {
  run bash analyzers/run-yourtool.sh "src/example.ext1"
  [[ "$status" -eq 0 ]]
  echo "$output" | jq -e 'length > 0'
}

@test "yourtool: returns empty array for no matching files" {
  run bash analyzers/run-yourtool.sh "README.md"
  [[ "$status" -eq 0 ]]
  [[ "$output" == "[]" ]]
}

@test "yourtool: returns empty array when binary missing" {
  unset YOURTOOL_MOCK_FILE
  PATH="/nonexistent" run bash analyzers/run-yourtool.sh "src/example.ext1"
  [[ "$status" -eq 0 ]]
  [[ "$output" == "[]" ]]
}
```

### 3. Wire it into review.sh

In `review.sh`, add the analyzer call alongside the existing ones. It runs concurrently with other analyzers in the parallel path:

```bash
# In the parallel block (search for "run-shellcheck.sh" to find the right spot):
run_analyzer "analyzers/run-yourtool.sh" "$CHANGED_FILES" "$YOURTOOL_OUTPUT" &

# In the sequential fallback block:
run_analyzer "analyzers/run-yourtool.sh" "$CHANGED_FILES" "$YOURTOOL_OUTPUT"
```

### 4. Update documentation

- Add a row to the analyzer table in `CLAUDE.md` (mock env var table)
- Add a row to the analyzer table in `README.md` and `docs/static-analyzers.md`
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#multi-provider-support-github--bitbucket-cloud--gitlab) for how the provider abstraction works.

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
- Functions are tested via `load_function` extraction in bats (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#test-architecture))
- Static analyzer scripts use the mock env var pattern for testing — never call real binaries in tests
- Findings JSON uses the schema documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#agent-output-schema)
