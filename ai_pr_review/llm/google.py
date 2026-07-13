"""Google Gemini provider (generativelanguage.googleapis.com).

Thinking tokens: Gemini 2.5 models emit thoughtsTokenCount billed at output
rate. We add them to output_tokens so the cost formula stays consistent, and
emit a THINKING: stderr line matching llm-call.sh behavior.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

import httpx

from ._config import get_retry_base_delay, get_retry_count, resolve_temperature
from ._http import LLMContentError, LLMError, retry_post
from .base import LLMRequest, LLMResponse

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _build_body(req: LLMRequest) -> dict[str, Any]:
    temperature = resolve_temperature(req.temperature, req.model_id)
    gen_config: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
    if temperature is not None:
        gen_config["temperature"] = temperature
    return {
        "system_instruction": {"parts": [{"text": req.system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": req.user_message}]}],
        "generationConfig": gen_config,
    }


async def call(req: LLMRequest) -> LLMResponse:
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise LLMError("GOOGLE_API_KEY is required for AI_PROVIDER=google")

    body = _build_body(req)
    encoded_model = urllib.parse.quote(req.model_id, safe="")
    url = f"{_BASE_URL}/{encoded_model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await retry_post(
            client,
            url,
            headers=headers,
            json=body,
            provider_label="Google Gemini",
            retry_count=get_retry_count(),
            retry_base_delay=get_retry_base_delay(),
        )

    return _parse_response(response.text, body)


def _parse_response(response_text: str, request_body: dict[str, Any]) -> LLMResponse:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Google returned non-JSON response ({exc}): {response_text[:200]}") from exc
    candidates = data.get("candidates", [])
    if not candidates:
        raise LLMError(f"Google returned no candidates: {response_text[:500]}")

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason", "")

    if finish_reason in ("SAFETY", "RECITATION"):
        raise LLMContentError(
            f"Response blocked by provider filter (finishReason={finish_reason})"
        )

    parts = candidate.get("content", {}).get("parts", [])
    text = next((p.get("text", "") for p in parts if "text" in p), "")
    if not text:
        raise LLMError(f"Could not extract response text from Google response: {response_text[:500]}")

    meta = data.get("usageMetadata", {})
    prompt_tokens = int(meta.get("promptTokenCount", 0))
    candidate_tokens = int(meta.get("candidatesTokenCount", 0))
    thinking_tokens = int(meta.get("thoughtsTokenCount", 0))
    cached_tokens = int(meta.get("cachedContentTokenCount", 0))

    # Add thinking tokens to output (billed at output rate).
    total_output = candidate_tokens + thinking_tokens

    # Gemini's promptTokenCount includes cachedContentTokenCount; subtract.
    uncached_input = max(prompt_tokens - cached_tokens, 0) if cached_tokens > 0 else prompt_tokens

    return LLMResponse(
        text=text,
        input_tokens=uncached_input,
        output_tokens=total_output,
        cache_creation_tokens=0,
        cache_read_tokens=cached_tokens,
        stop_reason=finish_reason,
        thinking_tokens=thinking_tokens,
        _request_body=str(request_body),
        _response_body=response_text,
        _provider="google",
    )
