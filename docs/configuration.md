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
| `LLM_RETRY_COUNT` | `3` | Retry attempts for transient LLM API failures (429, 5xx, timeouts). Set to `0` to disable. |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score (0–100) for findings. Findings below this are dropped before suppressions. |
| `AI_DISABLE_GATE_ARCHITECTURE` | `false` | Set to `true` to force `architecture-reviewer` to run even when the diff-heuristic gate would skip it (e.g., docs-only diffs). |
| `AI_DISABLE_GATE_SECURITY` | `false` | Set to `true` to force `security-reviewer` to run even when the diff-heuristic gate would skip it (e.g., diffs with no auth/crypto patterns or security-sensitive paths). |
| `AI_DISABLE_GATE_EDGE_CASE` | `false` | Set to `true` to force `edge-case-hunter` to run even when the diff-heuristic gate would skip it (e.g., diffs with no control-flow additions). |

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
