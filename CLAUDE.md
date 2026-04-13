# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions composite action that runs multiple LLM agents against a PR diff and posts a structured review (summary comment + inline findings) back to the PR. It is consumed as a git submodule by downstream repos.

## Key scripts and their roles

| Script | Role |
|--------|------|
| `review.sh` | Main orchestrator: diff computation, manifest building, language detection, agent dispatch, findings merge/dedup/suppress, invokes `post-review.sh` |
| `llm-call.sh` | Stateless curl-based LLM client; dispatches to the correct provider based on `AI_PROVIDER`; writes response to stdout, emits `TOKENS:` line to stderr |
| `post-review.sh` | GitHub API layer: resolves/dismisses stale review threads, posts summary comment, posts findings as a PR review with inline comments, advances SHA watermark |
| `run-shellcheck.sh` | Wraps shellcheck for changed `.sh`/`.bash` files; outputs findings in the same JSON schema as LLM agents |
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

`review.sh` uses `extract_findings()` to parse this block and validate shape. Findings below confidence 75 are filtered out. Duplicates on the same `file:line` are deduped (highest severity wins).

## Findings pipeline (review.sh phases)

1. **Phase 0** — Compute diff (incremental if SHA watermark found, else full PR diff). Exclude lockfiles, vendor dirs, node_modules.
2. **Phase 1** — Build shared message files (full context, code context, blind/no-context). Call agents via `call_agent()`.
3. **Phase 2** — Extract `json-findings` from each agent output. Merge with shellcheck findings. Apply `suppressions.json`. Filter by confidence ≥ 75. Deduplicate by `file:line`.
4. **Phase 3** — Format findings as markdown. Call `post-review.sh` to post everything to GitHub.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment embedded in the PR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). `post-review.sh --get-last-sha` extracts it. Subsequent pushes diff from that SHA to HEAD. `post-review.sh` calls `update_sha_marker()` at the end to advance it.

## Context message variants

Three message files are built and passed selectively to agents:

- **`FULL_CONTEXT_MSG`** — manifest + commit log + CLAUDE.md excerpt (first 2000 chars) + language profiles + diff
- **`CODE_CONTEXT_MSG`** — manifest + language profiles + diff (no commit log or project context)
- **`BLIND_MSG`** — raw diff only (intentional zero context for `blind-hunter`)

## Adding a new agent

1. Add a prompt file to `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` block.
2. In `review.sh`, call `call_agent "<name>" "$AI_MODEL_STANDARD|PREMIUM" "${SCRIPT_DIR}/prompts/<agent-name>.md" "<msg_var>" "<output_var>"` and push `<output_var>` onto `AGENT_OUTPUTS`.
3. If the agent should only run conditionally (like `silent-failure-hunter`), gate it with a grep check on `$DIFF_FILE`.

## Adding a language profile

Create `language-profiles/<language>.md` (filename must match the lowercase language key returned by `detect_language()` in `review.sh`). The file content is injected verbatim into `FULL_CONTEXT_MSG` and `CODE_CONTEXT_MSG` when that language is detected.

## Suppressions

`suppressions.json` is a JSON array of suppression rules evaluated against merged findings. All `match` fields are optional and ANDed:

- `file` — substring match on finding's `file`
- `line` — exact integer match
- `code` — finding text starts with this prefix
- `pattern` — regex (case-insensitive) matched against finding text

When adding a suppression, include an `id` and a `reason` explaining why it is a false positive.

## Provider model defaults (review.sh)

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-4-6-20250514` | `claude-opus-4-6-20250514` |
| `openai` / `openai-compatible` | `gpt-4o` | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-opus-4-6-v1` |

## Testing locally

There is no automated test suite. To test changes manually:

```bash
# Lint all shell scripts
shellcheck review.sh llm-call.sh post-review.sh run-shellcheck.sh

# Smoke-test llm-call.sh against a provider
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key>
echo "hello" > /tmp/msg.txt
echo "Say hi" > /tmp/sys.txt
./llm-call.sh claude-haiku-4-5-20251001 /tmp/sys.txt /tmp/msg.txt

# Dry-run review.sh (requires a real repo with a PR)
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> GH_TOKEN=<token>
export GITHUB_REPOSITORY=owner/repo PR_NUMBER=123 BASE_REF=main HEAD_SHA=<sha>
bash review.sh
```

## Release process

Run `/comprehensive-review` before tagging or releasing. This action is consumed as a submodule, so downstream repos pin to a specific commit or tag — breaking changes require a version bump and coordinated submodule updates in consuming repos.
