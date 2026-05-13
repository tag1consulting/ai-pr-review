"""Shared HTTP transport helpers for VCS providers.

Responsibilities:
- `retry_transient`: retry 502/503/429/timeouts with exponential backoff + jitter
- `TapeRecorder`: optional VCS tape recording honored via `AI_PR_REVIEW_RECORD_DIR`,
  producing the same JSON schema the Epic 0 harness expects.
- `redact_secrets`: scrub Authorization/Bearer tokens from tape bodies.

The providers dependency-inject an `httpx.Client` so tests can mock the transport
via `respx` without ever reaching the network.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import httpx

T = TypeVar("T")

_TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
_TRANSIENT_ERR_PATTERNS = re.compile(
    r"(ETIMEDOUT|Server Error|rate limit|Connection reset)", re.IGNORECASE
)

# Secret-like tokens we scrub from recorded tapes before they hit disk.
# Matches Authorization: Bearer xxx, Private-Token: xxx, ghp_, glpat-, etc.
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization|private-token|x-gitlab-token|x-api-key)\s*:[^\n\r]*"
)
_TOKEN_VALUE_RE = re.compile(
    r"\b(gh[psr]_[A-Za-z0-9]{10,}|glpat-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{10,})"
)


class RetryExhaustedError(httpx.HTTPError):
    """Raised when retry_transient runs out of attempts."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_backoff: float = 2.0
    jitter: bool = True
    # A hook for tests to override sleep; real callers use time.sleep.
    sleep: Callable[[float], None] = time.sleep


def _is_transient(exc: Exception | None, response: httpx.Response | None) -> bool:
    if response is not None and response.status_code in _TRANSIENT_STATUS:
        return True
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError):
        return True
    if response is not None:
        body = response.text or ""
        if _TRANSIENT_ERR_PATTERNS.search(body):
            return True
    return False


def retry_transient(
    func: Callable[[], httpx.Response],
    *,
    policy: RetryPolicy | None = None,
) -> httpx.Response:
    """Invoke `func` up to `policy.attempts` times on transient failures.

    Returns the first successful (non-transient) response. Raises
    `RetryExhaustedError` if every attempt is transient, or re-raises the
    underlying non-transient exception.
    """
    pol = policy or RetryPolicy()
    last_exc: Exception | None = None
    last_response: httpx.Response | None = None

    for attempt in range(1, pol.attempts + 1):
        try:
            resp = func()
        except Exception as exc:  # network/timeout errors come here
            last_exc = exc
            last_response = None
            if attempt >= pol.attempts or not _is_transient(exc, None):
                raise
        else:
            last_exc = None
            last_response = resp
            if not _is_transient(None, resp):
                return resp
            if attempt >= pol.attempts:
                break

        backoff = pol.base_backoff * (2 ** (attempt - 1))
        if pol.jitter:
            backoff += random.uniform(0, 1.0)
        pol.sleep(backoff)

    if last_response is not None:
        raise RetryExhaustedError(
            f"Retry exhausted after {pol.attempts} attempts; last status "
            f"{last_response.status_code}"
        )
    raise RetryExhaustedError(
        f"Retry exhausted after {pol.attempts} attempts; last error: {last_exc!r}"
    )


# ---------------------------------------------------------------------------
# Tape recording (Epic 0 golden harness compatibility)
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Scrub obvious credential patterns from a string before writing to disk."""
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)
    text = _TOKEN_VALUE_RE.sub("<redacted>", text)
    return text


@dataclass
class TapeRecorder:
    """Record HTTP requests/responses to a directory for golden-harness replay.

    When `record_dir` is None, all methods are no-ops. The tape filename schema
    matches Epic 0's GitHub tapes: `<seq>-<method>-<path-slug>.json`.
    """

    record_dir: Path | None
    provider: str = "github"
    _seq: int = 0

    @classmethod
    def from_env(cls, provider: str = "github") -> TapeRecorder:
        val = os.environ.get("AI_PR_REVIEW_RECORD_DIR", "")
        if not val:
            return cls(record_dir=None, provider=provider)
        path = Path(val)
        path.mkdir(parents=True, exist_ok=True)
        return cls(record_dir=path, provider=provider)

    def record(
        self,
        method: str,
        url: str,
        request_body: bytes | str | None,
        response: httpx.Response,
    ) -> None:
        if self.record_dir is None:
            return
        self._seq += 1
        req_text: str | None
        if isinstance(request_body, bytes):
            try:
                req_text = request_body.decode("utf-8")
            except UnicodeDecodeError:
                req_text = request_body.decode("utf-8", errors="replace")
        else:
            req_text = request_body
        if req_text is not None:
            req_text = redact_secrets(req_text)
        resp_text = redact_secrets(response.text or "")
        payload: dict[str, Any] = {
            "provider": self.provider,
            "seq": self._seq,
            "method": method.upper(),
            "url": url,
            "status": response.status_code,
            "request_body": req_text,
            "response_body": resp_text,
        }
        slug = _slugify_url(url)
        filename = f"{self._seq:04d}-{method.upper()}-{slug}.json"
        (self.record_dir / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def _slugify_url(url: str) -> str:
    # Strip scheme+host, collapse non-alnum to "-"
    without_scheme = re.sub(r"^https?://[^/]+", "", url)
    slug = _SLUG_RE.sub("-", without_scheme).strip("-")
    return slug[:80] or "root"


# ---------------------------------------------------------------------------
# Utility: a recording client wrapper used by providers
# ---------------------------------------------------------------------------


@dataclass
class RecordingClient:
    """Thin wrapper around `httpx.Client` that records every call to a tape.

    Providers call `.request(...)`; the wrapper runs the retry loop and feeds
    the outcome to the tape recorder.
    """

    http: httpx.Client
    recorder: TapeRecorder
    retry_policy: RetryPolicy = RetryPolicy()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        def _call() -> httpx.Response:
            return self.http.request(
                method, url, headers=headers, params=params, json=json_body
            )

        response = retry_transient(_call, policy=self.retry_policy)
        body_text = (
            json.dumps(json_body, sort_keys=True) if json_body is not None else None
        )
        self.recorder.record(method, url, body_text, response)
        return response
