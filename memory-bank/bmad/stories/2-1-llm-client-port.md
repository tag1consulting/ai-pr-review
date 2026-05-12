---
title: "E2.S1 — LLM client port (5 providers)"
epic: 2
story: 1
status: in-progress
github_issue: 216
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S1 — LLM client port (5 providers)

## Summary

Port `llm-call.sh` provider logic into `ai_pr_review/llm/` using official SDKs where available, `httpx` otherwise. Create a `client.py` router that dispatches based on `AI_PROVIDER`.

## PRD Reference

2.FR-1

## Acceptance Criteria

- [ ] 5 providers pass unit tests against tape-recorded responses (happy path + transient 429 retry fixture per provider)
- [ ] Anthropic `cache_control: ephemeral` markers preserved (shared-cache layout)
- [ ] OpenAI shared-cache request layout preserved
- [ ] Google thinking tokens extracted and added to output count for cost accounting
- [ ] Same cost-table output format as bash (TOKENS: line to stderr)
- [ ] `mypy --strict` and `ruff check` clean on all new code

## Files to Create

- `ai_pr_review/llm/__init__.py`
- `ai_pr_review/llm/base.py` — `LLMResponse` dataclass + `LLMProvider` protocol
- `ai_pr_review/llm/client.py` — router (`call_llm()`)
- `ai_pr_review/llm/anthropic.py`
- `ai_pr_review/llm/openai.py`
- `ai_pr_review/llm/google.py`
- `ai_pr_review/llm/bedrock.py`
- `ai_pr_review/llm/openai_compatible.py`
- `tests/python/llm/__init__.py`
- `tests/python/llm/test_anthropic.py`
- `tests/python/llm/test_openai.py`
- `tests/python/llm/test_google.py`
- `tests/python/llm/test_bedrock.py`
- `tests/python/llm/test_openai_compatible.py`
- `tests/python/llm/fixtures/` — tape-recorded JSON response fixtures

## Key Behaviors from llm-call.sh to Port

1. Temperature validation (0–2, clamped; some models skip temperature)
2. `LLM_PROMPT_CACHING` handling: auto/true/false, whitespace trimming
3. Anthropic shared-cache layout: user context in system[0] with cache_control:ephemeral, agent prompt in system[1]
4. OpenAI shared-cache layout: context + sentinel separator + agent instructions in system message
5. Google thinking tokens: extract + add to output_tokens, emit THINKING: line
6. Retry logic: exponential backoff, transient HTTP codes (408,429,500,502,503,504,520-524), `LLM_RETRY_COUNT`/`LLM_RETRY_BASE_DELAY`
7. TOKENS: stderr line: `input=N output=N cache_creation=N cache_read=N model=M`
8. TRUNCATED:true stderr on max_tokens stop reason
9. Tape recording: `AI_PR_REVIEW_RECORD_DIR` for fixture capture
10. model_supports_temperature() — skip for opus-4-7, o-series, gpt-5.5/5
11. Exit codes: 0=success, 1=permanent error, 2=transient (retries exhausted), 3=content filter

## Implementation Notes

- Use `httpx.AsyncClient` for async-native implementation
- `anthropic` SDK available via pip; use it for Anthropic (handles auth, retries internally OR use raw httpx to match bash exactly)
- Prefer raw `httpx` to match bash behavior exactly and avoid SDK abstraction differences
- The `LLMRequest` should mirror bash's interface: system_prompt_text + user_message_text + model_id + max_tokens
- `LLMResponse` should emit TOKENS line to stderr identical to bash format
