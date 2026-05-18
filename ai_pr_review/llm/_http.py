"""Shared httpx retry helper for all LLM providers.

Mirrors retry_curl() in llm-call.sh: exponential back-off with jitter on
transient HTTP codes (408, 429, 500, 502, 503, 504, 520–524) and transient
httpx exceptions (ConnectError, TimeoutException, NetworkError).
"""

from __future__ import annotations

import logging
import random
from typing import Any

import anyio
import httpx

logger = logging.getLogger(__name__)

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
                logger.warning(
                    "%s request failed (%s: %s), retrying in %.1fs (attempt %d/%d)...",
                    provider_label, type(exc).__name__, exc, delay + jitter, attempt, retry_count,
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
                logger.warning(
                    "%s returned HTTP %d (%s), retrying in %.1fs (attempt %d/%d)...",
                    provider_label, response.status_code,
                    response.text[:200] if response.content else "",
                    delay + jitter, attempt, retry_count,
                )
                await anyio.sleep(delay + jitter)
                continue
            raise LLMTransientError(
                f"{provider_label} returned HTTP {response.status_code} after "
                f"{retry_count} retries"
            )

        if response.status_code < 200 or response.status_code >= 300:
            logger.error("%s API returned HTTP %d", provider_label, response.status_code)
            try:
                logger.warning("%s error response body: %s", provider_label, response.text[:500])
            except Exception as log_exc:
                logger.warning(
                    "could not read error response body: %s; raw bytes: %r",
                    log_exc, response.content[:200],
                )
            raise LLMError(
                f"{provider_label} returned HTTP {response.status_code}: {response.text[:500]}"
            )

        return response
