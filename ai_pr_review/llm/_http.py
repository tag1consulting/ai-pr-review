"""Shared httpx retry helper for all LLM providers.

Mirrors retry_curl() in llm-call.sh: exponential back-off with jitter on
transient HTTP codes (408, 429, 500, 502, 503, 504, 520–524) and transient
httpx exceptions (ConnectError, TimeoutException, NetworkError).
"""

from __future__ import annotations

import random
import sys
from typing import Any

import anyio
import httpx

# HTTP status codes worth retrying.
_TRANSIENT_HTTP: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

# httpx exception types considered transient.
_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class LLMError(Exception):
    """Permanent LLM error (bad API key, invalid request, unknown provider)."""


class LLMTransientError(Exception):
    """Transient LLM error — all retries exhausted on 429/5xx/timeout."""


class LLMContentError(Exception):
    """Response blocked by provider safety/recitation filter."""


async def retry_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: Any,
    provider_label: str,
    retry_count: int,
    retry_base_delay: int,
) -> httpx.Response:
    """POST with exponential back-off retry on transient errors.

    Returns the successful Response. Raises LLMTransientError if all retries
    are exhausted, LLMError on permanent failures.
    """
    attempt = 0

    while True:
        try:
            response = await client.post(url, headers=headers, json=json)
        except _TRANSIENT_EXCEPTIONS as exc:
            if attempt < retry_count:
                attempt += 1
                delay = retry_base_delay * (2 ** (attempt - 1))
                jitter = random.random()
                print(
                    f"WARNING: {provider_label} request failed ({type(exc).__name__}), "
                    f"retrying in {delay + jitter:.1f}s (attempt {attempt}/{retry_count})...",
                    file=sys.stderr,
                )
                await anyio.sleep(delay + jitter)
                continue
            raise LLMTransientError(
                f"{provider_label} request failed after {retry_count} retries: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{provider_label} HTTP error: {exc}") from exc

        if response.status_code in _TRANSIENT_HTTP:
            if attempt < retry_count:
                attempt += 1
                delay = retry_base_delay * (2 ** (attempt - 1))
                jitter = random.random()
                print(
                    f"WARNING: {provider_label} returned HTTP {response.status_code}, "
                    f"retrying in {delay + jitter:.1f}s (attempt {attempt}/{retry_count})...",
                    file=sys.stderr,
                )
                await anyio.sleep(delay + jitter)
                continue
            raise LLMTransientError(
                f"{provider_label} returned HTTP {response.status_code} after "
                f"{retry_count} retries"
            )

        if response.status_code < 200 or response.status_code >= 300:
            print(
                f"ERROR: {provider_label} API returned HTTP {response.status_code}",
                file=sys.stderr,
            )
            try:
                print(response.text, file=sys.stderr)
            except Exception as log_exc:
                print(
                    f"WARNING: could not read error response body: {log_exc}; "
                    f"raw bytes: {response.content[:200]!r}",
                    file=sys.stderr,
                )
            raise LLMError(
                f"{provider_label} returned HTTP {response.status_code}: {response.text[:500]}"
            )

        return response
