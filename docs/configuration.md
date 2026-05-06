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
| `force-full-diff` | No | `false` | Bypass the SHA watermark; review the full PR diff for this run |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone` (GitHub issue) |
| `standalone-depth` | No | `''` | Commits to diff when base and head resolve to the same SHA |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for `pr` target; unused in standalone) |
| `base-ref` | **Yes** | — | Base branch name |
| `head-sha` | **Yes** | — | Head commit SHA |
| `github-token` | **Yes** | — | GitHub token with `pull-requests: write` |
| `retry-count` | No | `3` | Retry attempts for transient LLM failures |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). Set to `false` to revert to sequential if you hit provider rate limits |
| `confidence-threshold` | No | `75` | Minimum finding confidence score (0–100); findings below this are dropped |
| `max-inline` | No | `25` | Maximum inline review comments per run; excess routed to the review body |
| `max-tokens-per-agent` | No | `8192` | Max output tokens per LLM agent call (clamped to 256–65536) |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab; ignored on Bitbucket). Set to `false` to disable. |

## Supported LLM providers

| Provider | provider value | Required secret | Default models (standard / premium) |
|----------|-----------------|-----------------|--------------------------------------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` / `claude-opus-4-7` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-4o` / `gpt-4o` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-2.5-flash` / `gemini-2.5-pro` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-4-6` / `global.anthropic.claude-opus-4-7` |
