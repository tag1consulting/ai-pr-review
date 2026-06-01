# LLM Client Layer

## Key files
- `ai_pr_review/llm/client.py` — `call_llm()` top-level dispatcher; `_dispatch()` routes to provider
- `ai_pr_review/llm/base.py` — abstract base / shared types
- `ai_pr_review/llm/_config.py` — provider-specific config/defaults
- `ai_pr_review/llm/anthropic.py`, `openai.py`, `openai_compatible.py`, `google.py`, `bedrock.py` — provider implementations
- `ai_pr_review/llm/_http.py` — shared httpx async helpers

## Provider defaults (standard / premium)
| Provider | Standard | Premium |
|---|---|---|
| `anthropic` | `claude-sonnet-4-6` | `claude-opus-4-7` |
| `openai` | `gpt-5.4-mini` | `gpt-5.4` |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-opus-4-7` |

## Prompt caching
Anthropic and Bedrock paths support `cache_control: ephemeral` markers, gated by `LLM_PROMPT_CACHING` env var (default `auto`). Bash equivalent in `llm-call.sh`.

## Token accounting
`call_llm()` returns tokens used; rolled up into `ai_pr_review/pricing.py` cost table emitted at end of review.
