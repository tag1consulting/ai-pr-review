"""Ownership marker for VCS comments — resolves #183, #184.

The inline marker gates stale-cleanup. The GitHub and GitLab provider
implementations MUST only resolve/dismiss comments whose body contains
INLINE_MARKER, protecting other bots' reviews and threads from being touched
by our cleanup paths.

The summary marker format matches the bash engine so comments posted by bash
are still recognized by the Python engine.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Final

INLINE_MARKER: Final[str] = "<!-- ai-pr-review-inline -->"
SUMMARY_MARKER_PREFIX: Final[str] = "<!-- ai-pr-review-summary"
ID_MAP_MARKER_PREFIX: Final[str] = "<!-- ai-pr-review-id-map:"

_SHA_PATTERN = re.compile(r"\A[0-9a-f]{7,40}\Z")

# Matches a summary marker with optional sha= field, e.g.:
#   <!-- ai-pr-review-summary -->
#   <!-- ai-pr-review-summary sha=abc1234 -->
_SUMMARY_MARKER_RE = re.compile(
    r"<!-- ai-pr-review-summary(?:\s+sha=(?P<sha>[0-9a-f]+))?\s*-->"
)


def _is_valid_sha(sha: str) -> bool:
    return bool(_SHA_PATTERN.match(sha))


def build_summary_marker(head_sha: str) -> str:
    """Produce the summary marker, embedding head_sha when valid."""
    if _is_valid_sha(head_sha):
        return f"<!-- ai-pr-review-summary sha={head_sha} -->"
    return "<!-- ai-pr-review-summary -->"


def extract_summary_sha(body: str, context_hint: str = "") -> str | None:
    """Return the SHA from a summary marker, or None if missing/malformed.

    Logs a warning to stderr when a marker is present but the embedded SHA
    fails validation — that indicates corruption (marker was tampered with
    or written by a buggy caller) and the next incremental review will
    re-process from the PR base instead of the last watermark.

    Args:
        body: Comment body to scan.
        context_hint: Optional caller-supplied string (e.g., comment URL,
            comment id) included in any warning to aid debugging. When empty,
            the first 80 chars of `body` are included instead.
    """
    match = _SUMMARY_MARKER_RE.search(body)
    if not match:
        return None
    sha = match.group("sha")
    if not sha:
        return None
    if not _is_valid_sha(sha):
        hint = context_hint or body[:80].replace("\n", " ")
        print(
            f"WARNING: ai-pr-review summary marker contains invalid SHA {sha!r} "
            f"in {hint!r}; ignoring (next review will fall back to full diff)",
            file=sys.stderr,
        )
        return None
    return sha


def has_inline_marker(body: str) -> bool:
    """Case-sensitive check for the inline ownership marker."""
    return INLINE_MARKER in body


def has_summary_marker(body: str) -> bool:
    """Case-sensitive check for the summary marker (with or without sha=)."""
    return _SUMMARY_MARKER_RE.search(body) is not None


_ID_MAP_MARKER_RE = re.compile(
    r"<!-- ai-pr-review-id-map: (\{[^}]*\}) -->"
)


def build_id_map_marker(id_map: dict[str, int]) -> str:
    """Produce a hidden HTML comment embedding the finding ID map.

    The marker is machine-readable and invisible to users.  It is embedded
    in the review body so the ID map can be reconstructed from a single
    REST call to list reviews — no per-thread fetching required.

    Format: ``<!-- ai-pr-review-id-map: {"<fingerprint>": <id>, ...} -->``
    """
    payload = json.dumps(id_map, separators=(",", ":"), sort_keys=True)
    return f"<!-- ai-pr-review-id-map: {payload} -->"


def extract_id_map(body: str) -> dict[str, int]:
    """Extract the finding ID map from a review body.

    Returns an empty dict when no marker is present. Logs a warning and
    returns an empty dict when a marker is present but the JSON is malformed,
    so callers can distinguish "no marker" from "corrupt marker" via the log.

    Accepts both integer and whole-number float JSON values (e.g. ``1.0``)
    to tolerate serializer rounding.
    """
    import logging
    match = _ID_MAP_MARKER_RE.search(body)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
        if isinstance(data, dict):
            result: dict[str, int] = {}
            for k, v in data.items():
                if isinstance(v, int):
                    result[str(k)] = v
                elif isinstance(v, float) and v.is_integer():
                    result[str(k)] = int(v)
            return result
    except (json.JSONDecodeError, ValueError) as exc:
        logging.getLogger(__name__).warning(
            "ai-pr-review: id-map marker present but unparseable: %s", exc
        )
    return {}


def append_inline_marker(body: str) -> str:
    """Append INLINE_MARKER to body (idempotent)."""
    if has_inline_marker(body):
        return body
    if not body:
        return INLINE_MARKER
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}{INLINE_MARKER}"


def replace_summary_sha(body: str, new_sha: str, context_hint: str = "") -> str:
    """Replace the sha= field inside an existing summary marker.

    No-op when the body contains no summary marker or when new_sha is invalid.
    Only touches the match — surrounding text (including any unrelated
    `sha=...` substrings) is preserved. Logs a warning on each no-op so
    watermark-advance failures are observable.

    Args:
        body: Comment body to modify.
        new_sha: SHA to write into the marker's sha= field.
        context_hint: Optional caller-supplied string (e.g., comment URL,
            comment id) included in any warning to aid debugging. When empty,
            the first 80 chars of `body` are included instead.
    """
    if not _is_valid_sha(new_sha):
        hint = context_hint or body[:80].replace("\n", " ")
        print(
            f"WARNING: refusing to replace summary SHA with invalid value {new_sha!r} "
            f"({hint!r})",
            file=sys.stderr,
        )
        return body
    if not _SUMMARY_MARKER_RE.search(body):
        hint = context_hint or body[:80].replace("\n", " ")
        print(
            f"WARNING: replace_summary_sha called on body with no summary marker "
            f"({hint!r}); returning body unchanged",
            file=sys.stderr,
        )
        return body
    replacement = f"<!-- ai-pr-review-summary sha={new_sha} -->"
    return _SUMMARY_MARKER_RE.sub(replacement, body, count=1)
