# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions composite action that runs multiple LLM agents against a PR diff and posts a structured review (summary comment + inline findings) back to the PR. It is consumed by downstream repos either as a direct action reference (`uses: tag1consulting/ai-pr-review@main`) or as a git submodule.

## Key scripts and their roles

| Script | Role |
|--------|------|
| `review.sh` | Main orchestrator: diff computation, manifest building, language detection, agent dispatch, findings merge/dedup/suppress, invokes `post-review.sh` |
| `llm-call.sh` | Stateless curl-based LLM client; dispatches to the correct provider based on `AI_PROVIDER`; writes response to stdout, emits `TOKENS:` line to stderr |
| `post-review.sh` | GitHub API layer: resolves/dismisses stale review threads, posts summary comment, posts findings as a PR review with inline comments, advances SHA watermark |
| `run-shellcheck.sh` | Wraps shellcheck for changed `.sh`/`.bash` files; outputs findings in the same JSON schema as LLM agents |
| `run-cve-check.sh` | Queries [OSV.dev](https://osv.dev/) for known vulnerabilities in changed dependency manifests (`go.mod`, `package.json`, `requirements.txt`, `composer.json`); outputs findings in the same JSON schema as LLM agents |
| `action.yml` | GitHub Actions composite action definition; maps inputs to env vars and calls `review.sh` |

## Agent output schema

Every agent prompt expects a `json-findings` fenced code block in the response:

```json
[
  {
    "severity": "Critical|High|Medium|Low",
    "confidence": 0-100,
    "file": "path/to/file.ext",
    "line": 42,
    "finding": "Description of the issue",
    "remediation": "How to fix it"
  }
]
```

`review.sh` uses `extract_findings()` to parse this block and validate shape. Findings below confidence 75 are filtered out. Duplicates are deduped using proximity-based matching: findings in the same file within 3 lines of each other are merged into a single cluster, keeping the highest-severity finding.

## Findings pipeline (review.sh phases)

1. **Phase 0** — Compute diff (incremental if SHA watermark found, else full PR diff). Exclude lockfiles, vendor dirs, node_modules.
2. **Phase 1** — Build shared message files (full context, code context, blind/no-context). Call agents via `call_agent()`.
3. **Phase 2** — Extract `json-findings` from each agent output. Merge with shellcheck findings. Apply `suppressions.json`. Filter by confidence ≥ 75. Deduplicate using proximity-based matching (findings within 3 lines in the same file are merged).
4. **Phase 3** — Format findings as markdown. Call `post-review.sh` to post everything to GitHub.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment embedded in the PR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). `post-review.sh --get-last-sha` extracts it. Subsequent pushes diff from that SHA to HEAD. `post-review.sh` calls `update_sha_marker()` at the end to advance it.

To force a full-PR diff for a single run, add the `ai-review-rescan` label to the PR. This sets the `force-full-diff` action input to `true`, which causes `review.sh` to skip the `--get-last-sha` call (leaving `LAST_REVIEWED_SHA=""`) and fall through to the full `origin/BASE_REF...HEAD_SHA` diff. The watermark still advances normally at the end of the run.

## Context message variants

Three message files are built and passed selectively to agents:

- **`FULL_CONTEXT_MSG`** — manifest + commit log + CLAUDE.md excerpt (first 2000 chars) + language profiles + diff
- **`CODE_CONTEXT_MSG`** — manifest + language profiles + diff (no commit log or project context)
- **`BLIND_MSG`** — raw diff only (intentional zero context for `blind-hunter`)

## Adding a new agent

1. Add a prompt file to `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` block.
2. In `review.sh`, call `call_agent "<name>" "$AI_MODEL_STANDARD|PREMIUM" "${SCRIPT_DIR}/prompts/<agent-name>.md" "<msg_var>" "<output_var>" [max_tokens]` and push `<output_var>` onto `AGENT_OUTPUTS`. The optional 6th parameter `max_tokens` defaults to 16384.
3. If the agent should only run conditionally (like `silent-failure-hunter`), gate it with a grep check on `$DIFF_FILE`.

## Adding a language profile

Create `language-profiles/<language>.md` (filename must match the lowercase language key returned by `detect_language()` in `review.sh`). The file content is injected verbatim into `FULL_CONTEXT_MSG` and `CODE_CONTEXT_MSG` when that language is detected.

## CVE check

`run-cve-check.sh` inspects changed dependency manifests and queries OSV.dev for known vulnerabilities affecting the declared versions. Currently supports `go.mod`, `package.json`, `requirements.txt`, and `composer.json`.

The script follows the same external-tool pattern as `run-shellcheck.sh`: `review.sh` invokes it in Phase 1, captures a JSON findings array on stdout, and merges it through the standard pipeline (suppressions, confidence filter, dedup). Critical/High findings route to REQUEST_CHANGES through the existing severity ladder.

CVSS severity mapping:
- CVSS ≥ 9.0 → `Critical`, confidence 95
- CVSS 7.0–8.9 → `High`, confidence 95
- CVSS 4.0–6.9 → `Medium`, confidence 90
- CVSS < 4.0 or missing → `Low`, confidence 85

If OSV.dev is unreachable the script emits WARNING on stderr and returns `[]` — the review never blocks on CVE-check outage. Tests bypass the network via the `OSV_MOCK_FILE` env var which reads a canned response from a file; do not set this in production.

Suppressions work the same as for LLM findings — match on `pattern` against the finding text (e.g. `"CVE-2025-12345"` or `"GHSA-xxxx-yyyy-zzzz"`).

## Suppressions

`suppressions.json` is a JSON array of suppression rules evaluated against merged findings. All `match` fields are optional and ANDed:

- `file` — substring match on finding's `file`
- `line` — exact integer match
- `code` — finding text starts with this prefix
- `pattern` — regex (case-insensitive) matched against finding text

An optional `verify` field triggers pre-suppression verification before accepting the suppression:

| Value | Extracts | Checks |
|-------|----------|--------|
| `github-release` | `owner/repo@vN` | `gh api repos/{owner}/{repo}/git/ref/tags/{tag}` |
| `npm` | `pkg@version` or `"pkg": "version"` | `registry.npmjs.org/{pkg}/{version}` |
| `pypi` | `pkg==version` | `pypi.org/pypi/{pkg}/{version}/json` |
| `go-module` | `module@vX.Y.Z` | `proxy.golang.org/{module}/@v/{version}.info` |
| `cargo` | `pkg = "version"` or `pkg@version` | `crates.io/api/v1/crates/{pkg}/{version}` |
| `docker-hub` | `image:tag` or `ns/image:tag` | `hub.docker.com/v2/repositories/{ns}/{name}/tags/{tag}` |

If verification confirms the version exists, the suppression stands. If the API returns a non-zero exit (version not found), the finding is kept — the AI reviewer may be correct. Private registries (GHCR, GCR, ECR) are not supported as they require authentication.

When adding a suppression, include an `id` and a `reason` explaining why it is a false positive.

Consuming repos can add **local suppressions** by placing a `suppressions.json` file at `.github/ai-pr-review/suppressions.json` in their repository. Local rules are merged with the global rules at runtime — no action input required. Use the same schema as the global file.

## Provider model defaults (review.sh)

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-4-6` | `claude-opus-4-7` |
| `openai` / `openai-compatible` | `gpt-4o` | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-opus-4-7-v1` |

## Retry and resilience

`llm-call.sh` retries transient API failures (HTTP 429, 500, 502, 503) and transient curl failures (exit codes 7, 28, 56) with exponential backoff and jitter. Configuration via env vars:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RETRY_COUNT` | `3` | Number of retry attempts (set to 0 to disable) |
| `LLM_RETRY_BASE_DELAY` | `2` | Base delay in seconds (doubles each retry) |

The `retry-count` input in `action.yml` maps to `LLM_RETRY_COUNT`.

Additional env vars consumed by the scripts (not exposed as action inputs):

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TEMPERATURE` | `0.3` | Sampling temperature for LLM calls (clamped to [0, 2]) |
| `MAX_DIFF_LINES` | `5000` | Maximum diff lines before skipping review (mapped from `max-diff-lines` action input) |

Exit codes from `llm-call.sh`:
- **0** — success
- **1** — permanent error (bad API key, invalid request, unknown provider)
- **2** — transient error (all retries exhausted)
- **3** — content issue (provider safety/recitation filter blocked response)

`post-review.sh` wraps critical GitHub API calls with `gh_api_retry()` (3 retries, 2s/4s/8s backoff) for transient errors (502, 503, 429, ETIMEDOUT).

## Graceful failure handling

When an agent call fails (transient API error, content filter block, configuration issue), `call_agent()` in `review.sh`:
1. Logs a WARNING with the failure type and last error message
2. Appends the agent name to the `FAILED_AGENTS` array
3. Writes empty output and continues to the next agent

After all agents complete:
- If **all** finding agents failed, the review aborts with exit 1
- Otherwise, `FAILED_AGENTS` is exported as `AI_REVIEW_FAILED_AGENTS` (colon-separated) to `post-review.sh`
- `post-review.sh` downgrades an empty-findings review from APPROVE to COMMENT to indicate an incomplete review

## Token usage and cost estimation

`TOKEN_LOG` in `review.sh` accumulates `"agent: input=N output=N model=ID"` entries from `TOKENS:` lines emitted by `llm-call.sh` on stderr.

`model-pricing.json` maps model ID patterns to display names and per-token rates (cost per 1M tokens). The `model_pricing()`, `model_display_name()`, and `format_cost()` functions in `review.sh` use this data; `emit_token_table_rows()` generates the markdown table rows for the review comment and the Actions step summary.

## Testing locally

```bash
# Run the unit test suite (requires bats and jq)
bats tests/*.bats

# Lint all shell scripts
shellcheck review.sh llm-call.sh post-review.sh run-shellcheck.sh run-cve-check.sh

# Smoke-test llm-call.sh against a provider
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key>
echo "hello" > /tmp/msg.txt
echo "Say hi" > /tmp/sys.txt
./llm-call.sh claude-haiku-4-5 /tmp/sys.txt /tmp/msg.txt

# Dry-run review.sh (requires a real repo with a PR)
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> GH_TOKEN=<token>
export GITHUB_REPOSITORY=owner/repo PR_NUMBER=123 BASE_REF=main HEAD_SHA=<sha>
bash review.sh
```

## Standalone review mode

In addition to reviewing open PRs, the action supports a **standalone** mode that reviews any branch or commit SHA and posts findings as a GitHub issue rather than a PR review.

### Triggering standalone mode

Via `workflow_dispatch` in the GitHub UI or CLI:
- Leave `pr_number` empty (standalone is assumed when no PR number is provided)
- Optionally set `branch` to the branch to review (defaults to the repo's default branch)
- Diffs against the repo's default branch automatically

Programmatically:
```bash
export REVIEW_TARGET=standalone
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> GH_TOKEN=<token>
export GITHUB_REPOSITORY=owner/repo BASE_REF=main HEAD_SHA=<sha>
bash review.sh
```

### What changes in standalone mode

- No PR comment or PR review is posted
- All findings are posted as a single GitHub issue with severity, file:line references, and remediation notes
- The SHA watermark / incremental diff is skipped (always a full diff)
- Oversized diffs exit cleanly without posting a skip comment
- `post-review.sh` is invoked with `--standalone` instead of a PR number

### Issue output format

- Title: `🚨 AI Review: High risk — abc1234 on main`
- Body: overall risk summary, pr-summarizer output, findings sorted by severity, token usage table
- Labels: `ai-review` (always), `ai-review-action-needed` (Critical/High) — falls back gracefully if labels don't exist

## Test architecture

Tests live in `tests/` and use [bats-core](https://github.com/bats-core/bats-core). Because the scripts have no main guard (sourcing them triggers the full orchestration pipeline), `tests/test_helper.bash` extracts individual function definitions using an awk brace-depth tracker and `eval`s them into the test shell. This means:

- No production script changes are needed to make functions testable
- Tests cover pure functions from `review.sh` (`detect_language`, `model_pricing`, `model_display_name`, `format_cost`, `severity_icon`, `extract_findings`), from `llm-call.sh` (`is_transient_http`, `is_transient_curl`), and from `post-review.sh` (`gh_api_retry`)
- Fixture files in `tests/fixtures/` provide sample agent output for `extract_findings` tests

To add a test for a new function, call `load_function "$script" "function_name"` in the `setup()` block of the relevant `.bats` file.

## Release process

Run `/comprehensive-review` before tagging or releasing. This action is consumed via direct action reference (`@main`, `@v1.0`) or as a git submodule. Direct references pin to a branch or tag; submodule consumers pin to a specific commit. Breaking changes require a version bump and coordinated updates in consuming repos.
