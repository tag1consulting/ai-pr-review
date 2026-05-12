"""Multi-provider LLM router — Python port of llm-call.sh dispatch section.

Usage:
    response = asyncio.run(call_llm(request))
    response.emit_stderr(request.model_id)
    print(response.text)
"""

from __future__ import annotations

import sys

from ._config import resolve_prompt_caching
from ._http import LLMContentError, LLMError, LLMTransientError
from .base import LLMRequest, LLMResponse


async def call_llm(req: LLMRequest, provider: str) -> LLMResponse:
    """Dispatch to the correct provider and return an LLMResponse.

    Exit codes matching llm-call.sh:
      SystemExit(1) — permanent error
      SystemExit(2) — transient (retries exhausted)
      SystemExit(3) — content filter
    """
    try:
        return await _dispatch(req, provider)
    except LLMContentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    except LLMTransientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except LLMError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _dispatch(req: LLMRequest, provider: str) -> LLMResponse:
    if provider == "anthropic":
        from . import anthropic

        caching = resolve_prompt_caching(provider)
        return await anthropic.call(req, caching=caching)

    if provider in ("openai", "openai-compatible"):
        from . import openai

        return await openai.call(req, provider=provider)

    if provider == "google":
        from . import google

        return await google.call(req)

    if provider == "bedrock-proxy":
        from . import bedrock

        return await bedrock.call(req)

    raise LLMError(
        f"Unknown AI_PROVIDER '{provider}'. "
        "Valid values: anthropic | openai | openai-compatible | google | bedrock-proxy"
    )
