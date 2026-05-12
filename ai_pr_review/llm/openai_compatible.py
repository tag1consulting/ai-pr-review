"""openai-compatible provider — thin wrapper around openai.call with legacy layout."""

from __future__ import annotations

from .base import LLMRequest, LLMResponse
from .openai import call as _openai_call


async def call(req: LLMRequest) -> LLMResponse:
    return await _openai_call(req, provider="openai-compatible")
