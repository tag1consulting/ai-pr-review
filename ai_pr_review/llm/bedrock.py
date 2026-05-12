"""Bedrock proxy provider (Tag1 OpenWebUI proxy or similar).

Uses the Anthropic request shape with anthropic_version field, model in URL.
Reuses anthropic.build_body_for_bedrock() to keep the logic in one place.
"""

from __future__ import annotations

import json
import os
import urllib.parse

import httpx

from ._config import get_retry_base_delay, get_retry_count, resolve_prompt_caching
from ._http import LLMContentError, LLMError, retry_post
from .anthropic import build_body_for_bedrock
from .base import LLMRequest, LLMResponse


async def call(req: LLMRequest) -> LLMResponse:
    api_url = os.environ.get("BEDROCK_API_URL", "")
    api_key = os.environ.get("BEDROCK_API_KEY", "")
    if not api_url:
        raise LLMError("BEDROCK_API_URL is required for AI_PROVIDER=bedrock-proxy")
    if not api_key:
        raise LLMError("BEDROCK_API_KEY is required for AI_PROVIDER=bedrock-proxy")

    caching = resolve_prompt_caching("bedrock-proxy")
    body = build_body_for_bedrock(req, caching=caching)

    encoded_model = urllib.parse.quote(req.model_id, safe="")
    url = f"{api_url.rstrip('/')}/model/{encoded_model}/invoke"
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
            provider_label="Bedrock proxy",
            retry_count=get_retry_count(),
            retry_base_delay=get_retry_base_delay(),
        )

    return _parse_response(response.text, body)


def _parse_response(response_text: str, request_body: dict[str, object]) -> LLMResponse:
    data = json.loads(response_text)
    stop_reason = data.get("stop_reason", "")

    if stop_reason in ("SAFETY", "RECITATION", "refusal"):
        raise LLMContentError(f"Response blocked by provider filter (stop_reason={stop_reason})")

    content = data.get("content", [])
    text = next((c["text"] for c in content if c.get("type") == "text"), "")
    if not text:
        raise LLMError(
            f"Could not extract response text from Bedrock response: {response_text[:500]}"
        )

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
        _provider="bedrock-proxy",
    )
