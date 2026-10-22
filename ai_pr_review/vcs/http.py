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
import logging
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

import httpx

from ai_pr_review.logging import _mask_secrets, get_secret_literals
from ai_pr_review.output_clamp import clamp_request_body

T = TypeVar("T")

_log = logging.getLogger(__name__)

_TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
_TRANSIENT_ERR_PATTERNS = re.compile(
    r"(ETIMEDOUT|Server Error|rate limit|Connection reset)", re.IGNORECASE
)

# Secret-like tokens we scrub from recorded tapes before they hit disk.
# Matches Authorization: Bearer xxx, Private-Token: xxx, ghp_, glpat-, etc.
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization|private-token|x-gitlab-token|x-api-key|job-token)\s*:[^\n\r]*"
)
_TOKEN_VALUE_RE = re.compile(
    r"\b(gh[opsruv]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}"
    r"|glpat-[A-Za-z0-9_-]{10,}|glcbt-[A-Za-z0-9_-]{10,}"
    r"|sk-[A-Za-z0-9]{10,})"
)


# ---------------------------------------------------------------------------
# Outbound scrub (story 17-12, CAP-5)
# ---------------------------------------------------------------------------

# Backstop cap for one posted string. It must stay above every provider's own
# truncation limit (GitHub 65,536 bytes, GitLab 250,000, Bitbucket 32,000), so
# the provider logic runs first and this cap fires only if that logic is bypassed.
MAX_OUTBOUND_STRING_BYTES: Final[int] = 262_144

_CAP_NOTICE: Final[str] = "\n\n---\n*ai-pr-review: output cut at the outbound size limit.*"

# Spans that do not count toward the cap: the machine-readable markers (HTML
# comment form and the Bitbucket reference-link form) and suggestion fences.
_CAP_EXEMPT_RE = re.compile(
    r"<!-- ai-pr-review[^\n]*?-->"
    r"|\[//\]: # \(ai-pr-review[^\n)]*\)"
    r"|```suggestion[^\n]*\n.*?\n[ \t]*```",
    re.DOTALL,
)


# Exempt spans get their own budget, so a huge fence cannot skip the cap.
_EXEMPT_BUDGET_BYTES: Final[int] = 65_536

# A fence starts at the start of a line. An unanchored match could delete text in the middle of a line.
_SUGGESTION_RE = re.compile(r"(?m)^```suggestion[^\n]*\n.*?\n[ \t]*```", re.DOTALL)


def _drop_altered_suggestions(text: str) -> str:
    """Drop a suggestion fence that the scrub would change.

    A fence is code that GitHub commits when the author clicks Apply. A redacted fence
    would commit ``<redacted>`` into the file, so the whole fence goes instead.
    """

    def keep_or_drop(m: re.Match[str]) -> str:
        span = m.group()
        return span if scrub_outbound_text(span) == span else ""

    return _SUGGESTION_RE.sub(keep_or_drop, text)


def scrub_outbound_text(text: str) -> str:
    """Remove secret literals and token prefixes from a string the engine posts.

    Reuses ``logging._mask_secrets`` without its env-assignment layer, which
    would rewrite quoted code such as ``password = hash(x)`` in a review.
    """
    return _mask_secrets(text, secret_literals=get_secret_literals(), env_assignments=False)


def cap_outbound_text(text: str, limit: int = MAX_OUTBOUND_STRING_BYTES) -> str:
    """Cut ``text`` to ``limit`` UTF-8 bytes, not counting exempt spans.

    Markers and suggestion fences stay whole, also after the cut.
    """
    out: list[str] = []
    used = 0
    cut = False

    def take(chunk: str) -> None:
        nonlocal used, cut
        if cut or not chunk:
            return
        encoded = chunk.encode("utf-8")
        if used + len(encoded) <= limit:
            out.append(chunk)
            used += len(encoded)
            return
        room = max(0, limit - used)
        out.append(encoded[:room].decode("utf-8", errors="ignore") + _CAP_NOTICE)
        used = limit
        cut = True

    pos = 0
    exempt_used = 0
    for m in _CAP_EXEMPT_RE.finditer(text):
        take(text[pos : m.start()])
        span = m.group()
        size = len(span.encode("utf-8"))
        if exempt_used + size <= _EXEMPT_BUDGET_BYTES:
            exempt_used += size
            out.append(span)
        else:
            take(span)
        pos = m.end()
    take(text[pos:])
    if cut:
        _log.warning("outbound string cut at %d bytes", limit)
    return "".join(out)


def sanitize_outbound(value: Any, *, cap: bool = True) -> Any:
    """Scrub (and cap) every string inside a JSON-like request body."""
    if isinstance(value, str):
        scrubbed = scrub_outbound_text(_drop_altered_suggestions(value))
        return cap_outbound_text(scrubbed) if cap else scrubbed
    if isinstance(value, dict):
        return {k: sanitize_outbound(v, cap=cap) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_outbound(v, cap=cap) for v in value]
    return value


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
    if response is not None and response.status_code >= 500:
        # Only scan the body for transient-error phrases on 5xx responses.
        # Bitbucket (and others) echo the request payload back in 200 OK
        # response bodies, so e.g. a PR comment containing the literal
        # phrase "rate limit" or "Server Error" would have triggered a
        # bogus retry on a successful POST. e2e regression 2026-05-15.
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
    _seq: int = field(default=0, init=False, repr=False)

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
        # Every string the engine posts passes through here (story 17-12).
        if json_body is not None:
            # Scrub first, then the fork output clamp (story 17-11). The clamp must be the last step
            # that changes a posted string, because the scrub can delete a suggestion fence and so
            # join the text on both sides of it. No change outside a fork run's clamp scope.
            json_body = sanitize_outbound(json_body)
            if method.upper() in ("POST", "PATCH", "PUT"):
                json_body = clamp_request_body(json_body)

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

    def request_multipart(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        fields: Mapping[str, str],
    ) -> httpx.Response:
        """Send a ``multipart/form-data`` request (Bitbucket's ``/src`` endpoint,
        issue #906, is the sole caller: it takes plain string fields — branch
        name, ``parents``, commit message, and one field per file path whose
        value is the file's new content — as multipart parts, not JSON).

        Built as a bare ``httpx.Request`` and sent via ``self.http.send()``
        rather than ``self.http.request(..., files=...)``: a client
        constructed with a static default ``Content-Type`` header (every VCS
        client here sets ``application/json``, see e.g. `bitbucket.py`'s
        ``build_client``) does not let a per-request ``files=`` argument
        override that header when going through ``request()``/
        ``build_request()`` — httpx merges the client's default headers over
        the request's own before the multipart boundary is computed, so the
        request would go out as ``Content-Type: application/json`` with a
        multipart body, which Bitbucket's API silently fails to parse as
        form fields. Constructing the ``Request`` directly (not through the
        client) computes the correct ``multipart/form-data; boundary=...``
        header from scratch with nothing to collide with; ``client.send()``
        still applies the client's configured auth to a bare ``Request`` the
        same way ``client.request()`` would. Verified directly against a
        mock transport before relying on it here.
        """
        # httpx normalizes `base_url` to always carry a trailing slash (verified
        # directly: constructing a Client with base_url="https://x/2.0" reports
        # str(client.base_url) == "https://x/2.0/"), so naively concatenating a
        # leading-slash relative path produces a double slash the server may
        # not accept identically to the single-slash form. rstrip once here
        # rather than relying on every caller to pass a bare (no leading
        # slash) path.
        full_url = (
            url if url.startswith("http")
            else f"{str(self.http.base_url).rstrip('/')}{url}"
        )
        # Scrub only: feedback-store file content is bounded by its retention
        # logic, and a cap would corrupt it.
        fields = {k: scrub_outbound_text(v) for k, v in fields.items()}
        request = httpx.Request(
            method,
            full_url,
            headers=headers,
            files={k: (None, v) for k, v in fields.items()},
        )

        def _call() -> httpx.Response:
            return self.http.send(request)

        response = retry_transient(_call, policy=self.retry_policy)
        # Recorded for tape/debugging purposes only -- field names are kept
        # for tape readability, but every value (including file content) is
        # replaced with a redaction marker, since this is the feedback-store
        # write path and file content can carry PR-derived text (see this
        # module's own redact_secrets, which this omission makes
        # belt-and-suspenders for rather than the only guard).
        body_summary = json.dumps({k: "<omitted>" for k in fields}, sort_keys=True)
        self.recorder.record(method, url, body_summary, response)
        return response
