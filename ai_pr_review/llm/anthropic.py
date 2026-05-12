"""Anthropic provider — direct api.anthropic.com calls via httpx.

Preserves the shared-cache layout from llm-call.sh (_build_anthropic_body):
  When caching is enabled:
    system: [
      {type:"text", text:<user_message>, cache_control:{type:"ephemeral"}},
      {type:"text", text:<system_prompt>}
    ]
    messages: [{role:"user", content:"Please perform your review now."}]
  When caching is disabled (legacy layout):
    system: <system_prompt>
    messages: [{role:"user", content:<user_message>}]
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ._config import get_retry_base_delay, get_retry_count, resolve_temperature
from ._http import LLMContentError, LLMError, retry_post
from .base import LLMRequest, LLMResponse

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


def _build_body(
    req: LLMRequest,
    *,
    caching: bool,
    extra: dict[str, Any],
    include_model: bool = True,
) -> dict[str, Any]:
    temperature = resolve_temperature(req.temperature, req.model_id)
    body: dict[str, Any] = {**extra}
    if include_model:
        body["model"] = req.model_id
    if caching:
        body["system"] = [
            {"type": "text", "text": req.user_message, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": req.system_prompt},
        ]
        body["messages"] = [{"role": "user", "content": "Please perform your review now."}]
    else:
        body["system"] = req.system_prompt
        body["messages"] = [{"role": "user", "content": req.user_message}]
    body["max_tokens"] = req.max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    return body


async def call(req: LLMRequest, *, caching: bool) -> LLMResponse:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is required for AI_PROVIDER=anthropic")

    body = _build_body(req, caching=caching, extra={})
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await retry_post(
            client,
            _API_URL,
            headers=headers,
            json=body,
            provider_label="Anthropic",
            retry_count=get_retry_count(),
            retry_base_delay=get_retry_base_delay(),
        )

    return _parse_response(response.text, body)


def _parse_response(response_text: str, request_body: dict[str, Any]) -> LLMResponse:
    import json

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Anthropic returned non-JSON response ({exc}): {response_text[:200]}") from exc
    stop_reason = data.get("stop_reason", "")

    # Check content filter stop reasons before checking for empty text.
    if stop_reason in ("SAFETY", "RECITATION", "refusal"):
        raise LLMContentError(f"Response blocked by provider filter (stop_reason={stop_reason})")

    content = data.get("content", [])
    text = next((c["text"] for c in content if c.get("type") == "text"), "")
    if not text:
        raise LLMError(f"Could not extract response text from Anthropic response: {response_text[:500]}")

    usage = data.get("usage", {})
    return LLMResponse(
        text=text,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        stop_reason=stop_reason,
        _request_body=str(request_body),
        _response_body=response_text,
        _provider="anthropic",
    )


# ---------------------------------------------------------------------------
# Build helper reused by bedrock.py (same Anthropic request shape).
# ---------------------------------------------------------------------------
def build_body_for_bedrock(req: LLMRequest, *, caching: bool) -> dict[str, Any]:
    """Build an Anthropic-shaped body for Bedrock (model in URL, not body)."""
    return _build_body(
        req,
        caching=caching,
        extra={"anthropic_version": "bedrock-2023-05-31"},
        include_model=False,
    )
