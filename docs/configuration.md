---
layout: default
title: Configuration
nav_order: 2
---

# Configuration

## Action inputs

This table documents the root `action.yml` (direct-action) inputs. The container action (`container-action/action.yml`) mirrors all of these and adds `image-tag` (container image tag to pull), `registry-token` (deprecated, unused — the image is public), `fail-on-findings` (exit code 2 on `REQUEST_CHANGES`/`COMMENT`, for CI gating), and `context-max-queries` (cap on symbol lookups per run). See [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) for the container-action input set in context.

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `anthropic` | LLM provider |
| `api-key` | **Yes** | — | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `quick` | `quick` or `full` |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone`. Standalone currently only disables merge-commit filtering during diff computation — it does not post findings anywhere (issue-posting was part of the bash engine removed in v2.0.0 and was never reimplemented in Python; tracked in [issue #623](https://github.com/tag1consulting/ai-pr-review/issues/623)). |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). Set to `false` to revert to sequential if you hit provider rate limits |
| `temperature` | No | `0.3` | Sampling temperature for LLM calls (float in [0, 2]). Lower values produce more deterministic output. |
| `max-inline` | No | `25` | Maximum inline review comments per run; excess routed to the review body |
| `max-tokens-per-agent` | No | `16384` | Max output tokens per LLM agent call (clamped to [256, 65536]). Lowered from 32768 in v1.3.0. |
| `analyzer-concurrency` | No | `4` | Maximum simultaneous native static-analyzer subprocesses. Forced to 1 when `parallel: false`. Requires the Python engine. |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab; ignored on Bitbucket). Set to `false` to disable. |
| `ignore-merge-commits` | No | `true` | Strip merge commits that pulled in upstream base-branch changes before computing the diff. Only the PR author's own commits are reviewed. Falls back to the unfiltered diff if cherry-pick conflicts occur. Set to `false` to review all commits including upstream merges. |
| `sarif-paths` | No | `''` | Comma-separated SARIF 2.1.0 file paths (relative to workspace root) to merge into findings. Requires the Python engine. |
| `exclude-patterns` | No | `''` | Comma-separated git pathspec glob patterns to exclude from the diff (e.g. `docs/*,*.generated.go`). The `":!"` prefix is added automatically. Entries are split on commas; surrounding whitespace is trimmed and empty entries are dropped, so `docs/*, *.generated.go` is treated the same as `docs/*,*.generated.go`. Requires the Python engine. See `exclude-patterns-mode`. |
| `exclude-patterns-mode` | No | `append` | Controls how `exclude-patterns` interacts with the built-in excludes. `append` (default): user patterns are added to the built-in lockfile/vendor excludes. `replace`: only user patterns are used; built-in excludes are dropped. `replace` with an empty list falls back to the built-ins with a warning. Invalid values are rejected with an error. |
| `analyzers` | No | `''` | Allowlist: comma-separated names of static analyzers to run. When set, only the listed analyzers run and `exclude-analyzers` is ignored. Empty (default): all eligible analyzers run. Unknown names are rejected with an error. Requires the Python engine. See [Static analyzers](static-analyzers.md) for valid names. |
| `exclude-analyzers` | No | `''` | Denylist: comma-separated names of static analyzers to skip. Ignored when `analyzers` is set. Empty (default): no analyzers skipped. Unknown names are rejected with an error. Requires the Python engine. |
| `agents` | No | `''` | Allowlist: comma-separated names of review agents to run. When set, only the listed agents run and `exclude-agents` is ignored. Existing gates (mode, conditional triggers) still apply on top. Empty (default): all eligible agents run. Unknown names are rejected with an error. Requires the Python engine. See [Agents](agents.md) for valid names. |
| `exclude-agents` | No | `''` | Denylist: comma-separated names of review agents to skip. Ignored when `agents` is set. Excluding `pr-summarizer` suppresses the PR summary comment entirely. Empty (default): no agents skipped. Unknown names are rejected with an error. Requires the Python engine. |
| `analyzer-diff-scope` | No | `cap` | How out-of-diff native-analyzer findings are handled. `cap` (default): downgrade to Low severity and collapse into a `<details>` section so they don't trigger `REQUEST_CHANGES`. `drop`: remove them entirely. `off`: pass through unchanged. LLM-agent findings are never affected. Requires the Python engine. |

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
| `AI_REVIEW_MAX_TOKENS_PER_AGENT` | `16384` | `max-tokens-per-agent` | Output token budget per LLM agent call (clamped to 256–65536) |
| `AI_REVIEW_ENABLE_SUGGESTIONS` | `true` | `enable-suggestions` | Enable "Apply suggestion" buttons on inline comments |
| `AI_REVIEW_PARALLEL` | `true` | `parallel` | Run agents in parallel (tiered fan-out). Set `false` if you hit provider rate limits |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | `true` | `ignore-merge-commits` | Strip upstream base-branch merges from the diff before review |
| `AI_REVIEW_IMAGE_TAG` | `latest` | `image-tag` | Container image tag to pull (e.g. `latest`, `1.2.3`). Pin for reproducible runs. |
| `AI_REVIEW_CONTEXT_ENRICHMENT` | `true` (container), `false` (direct action) | `context-enrichment` | Inject tree-sitter symbol-context blocks into agent prompts (requires the Python engine, which is the default). The container image ships `tree-sitter-language-pack` and `ripgrep`, so enrichment is on by default there. Direct-action consumers without those tools get a silent no-op. |
| `AI_REVIEW_SARIF_PATHS` | `''` | `sarif-paths` | Comma-separated SARIF 2.1.0 file paths to merge into findings (requires the Python engine, which is the default). |
| `AI_REVIEW_EXCLUDE_PATTERNS` | `''` | `exclude-patterns` | Comma-separated git pathspec glob patterns to exclude from the diff (e.g. `docs/*`). Requires the Python engine, which is the default. |
| `AI_REVIEW_EXCLUDE_PATTERNS_MODE` | `append` | `exclude-patterns-mode` | How `exclude-patterns` interacts with built-in excludes: `append` (default) or `replace`. |
| `AI_REVIEW_FEEDBACK_LOOP` | `false` | `feedback-loop` / `enable-feedback-loop` | Enable the learning loop in both the main review workflow (inject `<repo-feedback>` block) and the slash-commands workflow (allow `/ai-pr-review false-positive`, `wont-fix`, `feedback`, `explain`, `revise` commands). GitHub-only. Requires the Python engine, which is the default. |
| `AI_REVIEW_ANALYZER_DIFF_SCOPE` | `cap` | `analyzer-diff-scope` | How out-of-diff native-analyzer findings are handled. `cap` (default): downgrade to Low and collapse under `<details>`. `drop`: remove entirely. `off`: pass through unchanged. Requires the Python engine. |

The `fail-on-findings` and `context-max-queries` container-action inputs (see [CI gate](#ci-gate-fail-on-findings)) are not currently wired into the shipped [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) template — there is no `AI_REVIEW_FAIL_ON_FINDINGS` or `AI_REVIEW_CONTEXT_MAX_QUERIES` repo variable read by that workflow. To use either, set them directly as `with:` values in your own copy of the workflow, or set the underlying `AI_FAIL_ON_FINDINGS` / `AI_CONTEXT_MAX_QUERIES` engine env vars.

To set a variable via the GitHub CLI:
```bash
gh variable set AI_REVIEW_PROVIDER --body "openai" --repo owner/repo
```

## Supported VCS providers

Select the VCS provider via the `VCS_PROVIDER` env var (default: `github`). This determines which VCS provider module is used and how findings are posted. `review-target: standalone` is not currently implemented for any provider (see [issue #623](https://github.com/tag1consulting/ai-pr-review/issues/623)) and is omitted from this table.

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval |
|----------|---------------|---------|--------|-------------|----------|
| GitHub | `github` (default) | Yes | Yes | Yes | Yes |
| Bitbucket Cloud | `bitbucket` | Yes (findings inside the summary body) | No | No | No |
| GitLab | `gitlab` | Yes | Yes | Yes | Yes |

See [Bitbucket setup](bitbucket-setup), [GitLab setup](gitlab-setup), or the [Getting Started](getting-started) page for provider-specific configuration.

## Supported LLM providers

| Provider | provider value | Required secret | Default models (standard / premium) |
|----------|-----------------|-----------------|--------------------------------------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5` / `claude-opus-4-8` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-5.4-mini` / `gpt-5.4` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-5` / `global.anthropic.claude-opus-4-7` |

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
| `STANDALONE_DEPTH` | `50` | Reserved for standalone review mode, which is not currently implemented (see [issue #623](https://github.com/tag1consulting/ai-pr-review/issues/623)). Not currently read outside config loading. |
| `LLM_RETRY_COUNT` | `2` | Retry attempts for transient LLM API failures (429, 5xx, timeouts). Set to `0` to disable. |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score (0–100) for findings. Findings below this are dropped before suppressions. |
| `AI_DISABLE_GATE_ARCHITECTURE` | `false` | Disables the docs-only heuristic gate; `architecture-reviewer` always runs regardless of diff content. |
| `AI_DISABLE_GATE_SECURITY` | `false` | Disables the keyword/path heuristic gate; `security-reviewer` always runs regardless of diff content. |
| `AI_DISABLE_GATE_EDGE_CASE` | `false` | Disables the control-flow heuristic gate; `edge-case-hunter` always runs regardless of diff content. |

### Legacy compatibility

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PR_REVIEW_COMPUTE_OUTPUT` | `''` | **Legacy.** Originally the Python compute phase wrote its payload here and the bash post-review scripts read it back. Since v0.9.0, the Python engine handles posting end-to-end and this handoff is no longer used by the action. Setting this still works for tooling that consumes the JSON directly. See [Compute Output Schema](compute-output-schema.md). |

### Opt-in capabilities

These variables enable optional capabilities that are off by default.

#### Context enrichment

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_CONTEXT_ENRICHMENT` | `true` (container), `false` (direct action) | Enable tree-sitter + ripgrep symbol-context injection. Extracts symbol references from the diff and appends relevant definitions to each agent's prompt in a `<symbol-context>` block. Requires `tree-sitter-language-pack` (included in the container image) and `ripgrep`. Silently no-ops if either dependency is absent. |
| `AI_CONTEXT_MAX_TOKENS` | `8192` | Maximum token budget for the injected `<symbol-context>` block per agent call. |
| `AI_CONTEXT_LOOKUP_LINES` | `8` | Number of source lines to capture per symbol definition (snippet window). |
| `AI_CONTEXT_MAX_QUERIES` | `200` | Maximum number of ripgrep symbol-lookup queries across all agents in a run. The cap is global (shared across Tier 1 and Tier 2 via the module-level cache), so a multi-agent run consumes queries faster than a single-agent one. Increase if you see `context enrichment: max_queries=N reached; remaining symbols skipped` in the logs. |

#### SARIF ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_SARIF_PATHS` | `''` | Comma-separated list of SARIF 2.1.0 file paths (relative to workspace root) to ingest as additional findings. Findings are merged into the same dedup/suppress pipeline as native analyzer results. Source tag: `sarif:<driver.name>`. |

#### Diff exclude patterns

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_EXCLUDE_PATTERNS` | `''` | Comma-separated list of git pathspec glob patterns to exclude from the diff before the LLM reads it (e.g. `docs/*,*.generated.go`). The `":!"` prefix is added automatically if missing. Entries are split on commas; surrounding whitespace is trimmed and empty entries are dropped, so `docs/*, *.generated.go` is treated the same as `docs/*,*.generated.go`. Use to reduce LLM token costs on large generated or documentation-only files. |
| `AI_EXCLUDE_PATTERNS_MODE` | `append` | Controls how `AI_EXCLUDE_PATTERNS` interacts with the built-in exclude list (lockfiles, `vendor/`, `node_modules/`). `append` (default): user patterns are appended after the built-ins. `replace`: only user patterns are used and the built-in list is dropped. Setting `replace` with an empty `AI_EXCLUDE_PATTERNS` logs a warning and falls back to the built-in list to avoid producing an unfiltered diff silently. Invalid values (anything other than `append` or `replace`) are rejected with an error at startup. |

#### Analyzer and agent selection

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_ANALYZERS` | `''` | Allowlist: comma-separated names of static analyzers to run. When set, only the listed analyzers run; `AI_EXCLUDE_ANALYZERS` is ignored. Empty (default): all eligible analyzers run. Unknown names are rejected at config load. Requires the Python engine. Valid names: `shellcheck`, `trufflehog`, `semgrep`, `ruff`, `golangci-lint`, `hadolint`, `checkov`, `phpcs`, `phpstan`, `eslint`, `kube-linter`, `tflint`, `cve-check`. |
| `AI_EXCLUDE_ANALYZERS` | `''` | Denylist: comma-separated names of static analyzers to skip. Ignored when `AI_ANALYZERS` is set. Empty (default): no analyzers skipped. Unknown names are rejected at config load. Requires the Python engine. |
| `AI_AGENTS` | `''` | Allowlist: comma-separated names of review agents to run. When set, only the listed agents run; `AI_EXCLUDE_AGENTS` is ignored. Existing gates (mode, conditional triggers) still apply on top. Empty (default): all eligible agents run. Unknown names are rejected at config load. Requires the Python engine. Valid names: `pr-summarizer`, `code-reviewer`, `silent-failure-hunter`, `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general`, `issue-linker`. |
| `AI_EXCLUDE_AGENTS` | `''` | Denylist: comma-separated names of review agents to skip. Ignored when `AI_AGENTS` is set. Excluding `pr-summarizer` suppresses the PR summary comment entirely. Empty (default): no agents skipped. Unknown names are rejected at config load. Requires the Python engine. |

#### Static analyzer options

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_ANALYZER_CONCURRENCY` | `4` | Maximum simultaneous native static-analyzer subprocesses. Forced to 1 when `AI_PARALLEL=false`. Requires the Python engine. |
| `AI_ANALYZER_DIFF_SCOPE` | `cap` | Controls how out-of-diff native static-analyzer findings are handled (requires the Python engine). Native analyzers (phpcs, phpstan, ruff, golangci-lint, etc.) lint entire files, so a small change can produce many diagnostics on unchanged lines. `cap` (default): downgrade those findings to Low severity and collapse them into a `<details>` section in the summary comment — they remain visible but do not trigger `REQUEST_CHANGES`. `drop`: remove out-of-diff analyzer findings entirely. `off`: pass through unchanged (full-file linting behaviour, pre-v1.2 default). LLM-agent findings are never affected regardless of this setting. |

#### Learning loop

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_FEEDBACK_LOOP` | `false` | Enable the learning loop. Loads recent feedback from the GitBranchStore and injects a `<repo-feedback>` block into agent prompts. GitHub-only (GitLab/Bitbucket stub returns no-op). |
| `AI_FEEDBACK_BRANCH` | `ai-pr-review-bot` | Git branch used to persist the feedback JSONL file. The branch is created automatically on first write. Requires `GH_TOKEN` with `contents:write` on this branch. |
| `AI_FEEDBACK_MAX_TOKENS` | `2048` | Maximum token budget for the injected `<repo-feedback>` block. |
| `AI_FEEDBACK_RETENTION_COUNT` | `500` | Maximum number of feedback entries to keep (rolling window; oldest dropped first). |
| `AI_FEEDBACK_RETENTION_AGE_DAYS` | `365` | Drop entries older than this many days. Set to `0` to disable age-based pruning. |

#### CI gate (fail on findings)

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_FAIL_ON_FINDINGS` | `false` | When `true`, exit with code 2 if the review outcome is `REQUEST_CHANGES` or `COMMENT` (incomplete or unknown risk). Exit code 1 still signals a posting or config error. Exit code 0 means the bot approved. Use this to block auto-merge or required CI checks until the bot approves. Pair with branch protection requiring the `review` status check and set `fail-on-findings: true` in your workflow. |

#### Judge pass

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_JUDGE_PASS` | `true` | Run a cheap-model judge pass (Phase 2.75) after findings are extracted, merged, suppressed, and scoped. The judge sends a single compact LLM call (no diff text) and returns `keep` or `downrank` per finding. `downrank` lowers the finding's confidence by 15 points and routes it to the review body instead of as an inline comment — the finding is still reported. Corroborated findings (static-analyzer + LLM-agent agreement on the same file+line) are exempt from `downrank`. Always fail-soft: any judge error returns findings unchanged. The judge call's token usage appears as a `judge-pass` row in the token usage table and is included in the Total. Set to `false` to disable and restore pre-v2.1 behavior. |

#### Per-agent language-profile routing

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROFILE_MAX_TOKENS` | `4096` | Maximum token budget for per-agent language-profile context. Each eligible agent receives only the profile sections relevant to its review focus (`security`, `bugs`, `edge`, `idioms`, `general`), packed under this budget. Reduce to lower token spend on multi-language PRs; increase if profile context is being truncated in telemetry. |

> **Incremental reviews and gates:** The gates evaluate the *incremental* diff (SHA watermark → HEAD), not the full PR diff. On a PR where an initial commit adds security-relevant code and a later commit only updates docs, the follow-up run will skip `security-reviewer`. Use `AI_DISABLE_GATE_SECURITY=true` (or apply the `ai-review-rescan` PR label with `FORCE_FULL_DIFF`) on security-sensitive PRs to ensure all Tier-2 agents run on every update.

### Telemetry hooks

These variables configure the telemetry system. Telemetry is fail-soft: all I/O errors are logged as warnings and the review continues.

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

These variables configure the logging system. Set them in your workflow `env:` block.

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
