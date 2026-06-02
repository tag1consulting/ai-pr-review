---
layout: default
title: Configuration
nav_order: 2
---

# Configuration

## Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `anthropic` | LLM provider |
| `api-key` | **Yes** | — | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `quick` | `quick` or `full` |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone` (GitHub issue) |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). Set to `false` to revert to sequential if you hit provider rate limits |
| `max-inline` | No | `25` | Maximum inline review comments per run; excess routed to the review body |
| `max-tokens-per-agent` | No | `8192` | Max output tokens per LLM agent call (clamped to 256–65536). Gemini defaults to `16384` when not set (thinking tokens consume the output budget). |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab; ignored on Bitbucket). Set to `false` to disable. |
| `ignore-merge-commits` | No | `false` | Strip merge commits that pulled in upstream base-branch changes before computing the diff. Only the PR author's own commits are reviewed. Falls back to the unfiltered diff if cherry-pick conflicts occur. |

## Repository variables

These optional variables can be set in **Settings → Secrets and variables → Actions → Variables** of the consuming repository. The example workflows read them with a fallback default so the workflow file never needs to be edited for routine configuration changes.

| Variable | Default | Corresponding input | Description |
|----------|---------|---------------------|-------------|
| `AI_REVIEW_API_KEY` | — | `api-key` | **(Secret)** API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | `anthropic` | `provider` | LLM provider name |
| `AI_REVIEW_BASE_URL` | `''` | `base-url` | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Per-provider default | `model-standard` | Override the standard agent model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Per-provider default | `model-premium` | Override the premium agent model ID (full mode only) |
| `AI_REVIEW_MAX_DIFF_LINES` | `5000` | `max-diff-lines` | Skip review when diff exceeds this many lines |
| `AI_REVIEW_MAX_INLINE` | `25` | `max-inline` | Max inline comments per run; excess routed to the summary body |
| `AI_REVIEW_MAX_TOKENS_PER_AGENT` | `8192` | `max-tokens-per-agent` | Output token budget per LLM agent call (clamped to 256–65536) |
| `AI_REVIEW_ENABLE_SUGGESTIONS` | `true` | `enable-suggestions` | Enable "Apply suggestion" buttons on inline comments |
| `AI_REVIEW_PARALLEL` | `true` | `parallel` | Run agents in parallel (tiered fan-out). Set `false` if you hit provider rate limits |
| `AI_PR_REVIEW_ENGINE` | `python` | `engine` | Compute engine: `python` (default) or `bash` (deprecated legacy; will be removed in a future major release) |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | `false` | `ignore-merge-commits` | Strip upstream base-branch merges from the diff before review |
| `AI_REVIEW_IMAGE_TAG` | `latest` | `image-tag` | Container image tag to pull (e.g. `latest`, `1.2.3`). Pin for reproducible runs. |
| `AI_REVIEW_CONTEXT_ENRICHMENT` | `false` | `context-enrichment` | Inject tree-sitter symbol-context blocks into agent prompts (requires `engine: python`). |
| `AI_REVIEW_SARIF_PATHS` | `''` | `sarif-paths` | Comma-separated SARIF 2.1.0 file paths to merge into findings (requires `engine: python`). |
| `AI_REVIEW_FEEDBACK_LOOP` | `false` | `feedback-loop` / `enable-feedback-loop` | Enable the learning loop in both the main review workflow (inject `<repo-feedback>` block) and the slash-commands workflow (allow `/ai-pr-review false-positive`, `wont-fix`, `feedback`, `explain`, `revise` commands). GitHub-only. Requires `engine: python`. |

To set a variable via the GitHub CLI:
```bash
gh variable set AI_REVIEW_PROVIDER --body "openai" --repo owner/repo
```

## Supported VCS providers

Select the VCS provider via the `VCS_PROVIDER` env var (default: `github`). This determines which post-review script is used and how findings are posted.

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval | Standalone |
|----------|---------------|---------|--------|-------------|----------|------------|
| GitHub | `github` (default) | Yes | Yes | Yes | Yes | Yes |
| Bitbucket Cloud | `bitbucket` | Yes | No | No | No | No |
| GitLab | `gitlab` | Yes | Yes | Yes | Yes | Yes |

See [Bitbucket setup](bitbucket-setup), [GitLab setup](gitlab-setup), or the [Getting Started](getting-started) page for provider-specific configuration.

## Supported LLM providers

| Provider | provider value | Required secret | Default models (standard / premium) |
|----------|-----------------|-----------------|--------------------------------------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` / `claude-opus-4-7` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-5.4-mini` / `gpt-5.4` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-4-6` / `global.anthropic.claude-opus-4-7` |

## Environment variables

These variables are consumed by the scripts but not exposed as action inputs. Set them in
your workflow `env:` block or pass them via `docker run -e`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TEMPERATURE` | `0.3` | Sampling temperature for LLM calls (clamped to [0, 2]) |
| `LLM_PROMPT_CACHING` | `auto` | Enable Anthropic/Bedrock prompt caching. `auto` enables for anthropic and bedrock-proxy; `true` force-enables; `false` force-disables. |
| `AI_CACHE_PRIMING` | `false` | Serialize cache-writing calls before parallel fan-out. Default off (opportunistic hits suffice). Enable in rate-limited or serialized-proxy environments. |
| `VCS_PROVIDER` | `github` | Selects the post-review script. Valid: `github`, `bitbucket`, `gitlab`. |
| `PHPSTAN_LEVEL` | `3` | PHPStan analysis depth level (0-9); ignored if the project has `phpstan.neon` or `phpstan.neon.dist` |

### Advanced tuning (env-var only)

These settings were previously action inputs. They still work as environment
variables — set them in your workflow `env:` block. Most consumers never need
to change them.

| Variable | Default | Description |
|----------|---------|-------------|
| `FORCE_FULL_DIFF` | `false` | Bypass the SHA watermark and review the full PR diff. Prefer the `ai-review-rescan` PR label instead — it sets this automatically. |
| `STANDALONE_DEPTH` | `''` | In standalone mode, diff the last N commits when base and head resolve to the same SHA. If unset, diffs the entire tree. |
| `LLM_RETRY_COUNT` | `3` (bash) / `2` (python) | Retry attempts for transient LLM API failures (429, 5xx, timeouts). Set to `0` to disable. |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score (0–100) for findings. Findings below this are dropped before suppressions. |
| `AI_DISABLE_GATE_ARCHITECTURE` | `false` | Disables the docs-only heuristic gate; `architecture-reviewer` always runs regardless of diff content. |
| `AI_DISABLE_GATE_SECURITY` | `false` | Disables the keyword/path heuristic gate; `security-reviewer` always runs regardless of diff content. |
| `AI_DISABLE_GATE_EDGE_CASE` | `false` | Disables the control-flow heuristic gate; `edge-case-hunter` always runs regardless of diff content. |

### Legacy compatibility

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PR_REVIEW_COMPUTE_OUTPUT` | `''` | **Legacy.** Originally the Python compute phase wrote its payload here and the bash post-review scripts read it back. Since v0.9.0, the Python engine handles posting end-to-end and this handoff is no longer used by the action. Setting this still works for tooling that consumes the JSON directly. See [Compute Output Schema](compute-output-schema.md). |

### Opt-in capabilities

These variables enable optional capabilities that are off by default. All require the Python engine (the default since v1.0.0; set explicitly with `AI_PR_REVIEW_ENGINE=python` if you have overridden the default) unless stated otherwise.

#### Context enrichment

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_CONTEXT_ENRICHMENT` | `false` | Enable tree-sitter + ripgrep symbol-context injection. Extracts symbol references from the diff and appends relevant definitions to each agent's prompt in a `<symbol-context>` block. Requires `tree-sitter-language-pack` (included in the container image) and `ripgrep`. |
| `AI_CONTEXT_MAX_TOKENS` | `8192` | Maximum token budget for the injected `<symbol-context>` block per agent call. |
| `AI_CONTEXT_LOOKUP_LINES` | `8` | Number of source lines to capture per symbol definition (snippet window). |

#### SARIF ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_SARIF_PATHS` | `''` | Comma-separated list of SARIF 2.1.0 file paths (relative to workspace root) to ingest as additional findings. Findings are merged into the same dedup/suppress pipeline as native analyzer results. Source tag: `sarif:<driver.name>`. |

#### Learning loop

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_FEEDBACK_LOOP` | `false` | Enable the learning loop. Loads recent feedback from the GitBranchStore and injects a `<repo-feedback>` block into agent prompts. GitHub-only (GitLab/Bitbucket stub returns no-op). |
| `AI_FEEDBACK_BRANCH` | `ai-pr-review-bot` | Git branch used to persist the feedback JSONL file. The branch is created automatically on first write. Requires `GH_TOKEN` with `contents:write` on this branch. |
| `AI_FEEDBACK_MAX_TOKENS` | `2048` | Maximum token budget for the injected `<repo-feedback>` block. |
| `AI_FEEDBACK_RETENTION_COUNT` | `500` | Maximum number of feedback entries to keep (rolling window; oldest dropped first). |
| `AI_FEEDBACK_RETENTION_AGE_DAYS` | `365` | Drop entries older than this many days. Set to `0` to disable age-based pruning. |

> **Incremental reviews and gates:** The gates evaluate the *incremental* diff (SHA watermark → HEAD), not the full PR diff. On a PR where an initial commit adds security-relevant code and a later commit only updates docs, the follow-up run will skip `security-reviewer`. Use `AI_DISABLE_GATE_SECURITY=true` (or apply the `ai-review-rescan` PR label with `FORCE_FULL_DIFF`) on security-sensitive PRs to ensure all Tier-2 agents run on every update.

### Telemetry hooks

These variables configure the Python engine's telemetry system (`engine: python` only). Telemetry is fail-soft: all I/O errors are logged as warnings and the review continues.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TELEMETRY_ENABLED` | `false` | Set to `true` to emit a structured JSON event per review run. |
| `AI_TELEMETRY_SINK` | — | Where to send telemetry. Accepts `file:///absolute/path/events.jsonl` (appended line-by-line) or an `http(s)://` endpoint (POST). |

Each event (schema version `"2"`) includes:

- Identity and run context: `correlation_id`, `timestamp`, `repository`, `pr_number`, `telemetry_schema_version`.
- Outcome: `outcome` (one of `"success"`, `"failure"`, `"skipped"`, `"dry_run"`), `findings_count`, `findings_by_severity`, `failed_agents`.
- Per-agent metrics: `token_usage_by_agent`, `agent_latency_ms`, `failed_agent_latency_ms`.
- Configuration shape (v2 additions): `provider`, `model_standard`, `model_premium`, `review_mode`, `is_incremental`.
- Other: `sarif_elapsed_s`, `learning_store_entries_loaded`.

All v2 additions are forward-compatible: consumers parsing v1 events that ignore unknown keys continue to work. Consumers that switch on `telemetry_schema_version` should add `"2"` to their accepted set, and consumers that switch on `outcome` should handle the new `"skipped"` and `"dry_run"` values.

### Structured logging

These variables configure the Python engine's logging system (`engine: python` only). Set them in your workflow `env:` block.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_LOG_FORMAT` | `human` | Log output format. `human` — human-readable timestamped lines. `json` — machine-readable JSON objects with `timestamp`, `level`, `logger`, `correlation_id`, and `message` fields; suitable for Datadog, CloudWatch, and similar log aggregators. |
| `AI_LOG_LEVEL` | `WARNING` | Minimum log level to emit. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive). |
| `AI_PR_REVIEW_CORRELATION_ID` | *(auto-generated)* | 8-character hex correlation ID injected into every log record for the duration of a review run. Auto-generated at startup; set explicitly to correlate logs across multiple jobs. |

Secret masking is always active: API keys, tokens, and other credentials from `ReviewConfig` are redacted from log output regardless of log format or level.

### Bitbucket-specific variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BITBUCKET_EMAIL` | — | Atlassian account email of the bot user (Basic-auth username) |
| `BITBUCKET_API_TOKEN` | — | Atlassian API token (Basic-auth password) |
| `BITBUCKET_WORKSPACE` | — | Optional explicit override for workspace slug; defaults to splitting `GITHUB_REPOSITORY` |
| `BITBUCKET_REPO_SLUG` | — | Optional explicit override for repo slug |

### GitLab-specific variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GITLAB_TOKEN` | — | Personal or project access token with `api` scope; falls back to `CI_JOB_TOKEN` |
| `GITLAB_API_URL` | `https://gitlab.com/api/v4` | API base URL for self-hosted instances |
| `GITLAB_PROJECT_ID` | — | Numeric project ID; falls back to `CI_PROJECT_ID`, then URL-encodes `CI_PROJECT_PATH` or `GITHUB_REPOSITORY` |
| `GITLAB_MR_DIFF_BASE_SHA` | — | Base SHA for inline discussion positions; falls back to `CI_MERGE_REQUEST_DIFF_BASE_SHA` |
| `GITLAB_BOT_USERNAME` | — | Username of the bot posting reviews (for stale thread resolution); defaults to the authenticated user |
