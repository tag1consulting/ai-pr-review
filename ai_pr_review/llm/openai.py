"""OpenAI and openai-compatible provider.

Shared-cache message layout (AI_PROVIDER=openai only):
  Puts the shared user context FIRST in the system message so OpenAI's
  automatic prefix caching sees a common prefix across all agents.
  system: "<user_message>\\n\\n===AGENT_INSTRUCTIONS===\\n\\n...\\n\\n<system_prompt>"
  messages: [{role:"user", content:"Please perform your review now."}]

  OpenAI's prefix caching is automatic (no per-request opt-in), so the layout
  switch is controlled by LLM_PROMPT_CACHING only (default "auto" = enabled).
  This is independent of the cache_control headers used by Anthropic/Bedrock.

openai-compatible endpoints keep the legacy layout (system=prompt, user=context)
and use max_tokens instead of max_completion_tokens.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ._config import get_retry_base_delay, get_retry_count, resolve_temperature
from ._http import LLMContentError, LLMError, retry_post
from .base import LLMRequest, LLMResponse

_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


def _use_shared_layout(provider: str) -> bool:
    """Use the shared-cache message layout for first-party openai, unless disabled.

    OpenAI's prefix caching is automatic (no per-request opt-in), so this flag
    controls only the *message layout* — it is intentionally independent of
    resolve_prompt_caching(), which governs explicit cache_control headers for
    Anthropic/Bedrock. "auto" enables the layout for openai; "false"/"0" disables it.
    """
    if provider != "openai":
        return False
    raw = os.environ.get("LLM_PROMPT_CACHING", "auto").strip().lower()
    return raw not in ("false", "0")


def _build_body(req: LLMRequest, *, provider: str) -> dict[str, Any]:
    temperature = resolve_temperature(req.temperature, req.model_id)
    shared = _use_shared_layout(provider)
    # openai-compatible uses max_tokens; first-party openai uses max_completion_tokens.
    token_field = "max_tokens" if provider == "openai-compatible" else "max_completion_tokens"

    if shared:
        system_text = (
            req.user_message
            + "\n\n===AGENT_INSTRUCTIONS===\n\n"
            "You are a specialized review agent. Follow these instructions:\n\n"
            + req.system_prompt
        )
        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": "Please perform your review now."},
        ]
    else:
        messages = [
            {"role": "system", "content": req.system_prompt},
            {"role": "user", "content": req.user_message},
        ]

    body: dict[str, Any] = {
        "model": req.model_id,
        "messages": messages,
        token_field: req.max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature
    return body


async def call(req: LLMRequest, *, provider: str) -> LLMResponse:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise LLMError(f"OPENAI_API_KEY is required for AI_PROVIDER={provider}")

    base_url = os.environ.get("OPENAI_BASE_URL", "")
    if provider == "openai-compatible" and not base_url:
        raise LLMError(
            "AI_PROVIDER=openai-compatible requires base-url (OPENAI_BASE_URL); "
            "set the action input `base-url` for your endpoint, or use "
            "AI_PROVIDER=openai to call api.openai.com."
        )
    if not base_url:
        base_url = "https://api.openai.com/v1"
    url = f"{base_url.rstrip('/')}/chat/completions"

    body = _build_body(req, provider=provider)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await retry_post(
            client,
            url,
            headers=headers,
            json=body,
            provider_label="OpenAI",
            retry_count=get_retry_count(),
            retry_base_delay=get_retry_base_delay(),
        )

    return _parse_response(response.text, body)


def _parse_response(response_text: str, request_body: dict[str, Any]) -> LLMResponse:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"OpenAI returned non-JSON response ({exc}): {response_text[:200]}") from exc
    choices = data.get("choices", [])
    if not choices:
        raise LLMError(f"OpenAI returned no choices: {response_text[:500]}")

    choice = choices[0]
    stop_reason = choice.get("finish_reason", "")

    if stop_reason in ("content_filter", "refusal"):
        raise LLMContentError(f"Response blocked by provider filter (finish_reason={stop_reason})")

    content = choice.get("message", {}).get("content") or ""
    if not content:
        raise LLMError(f"Could not extract response text from OpenAI response: {response_text[:500]}")

    usage = data.get("usage", {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    cached_tokens = int(
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    )

    # OpenAI prompt_tokens includes cached_tokens; subtract to match Anthropic convention.
    uncached_input = max(prompt_tokens - cached_tokens, 0)

    return LLMResponse(
        text=content,
        input_tokens=uncached_input,
        output_tokens=completion_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=cached_tokens,
        stop_reason=stop_reason,
        _request_body=str(request_body),
        _response_body=response_text,
        _provider="openai",
    )
