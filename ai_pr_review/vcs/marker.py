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
import logging
import re
import sys
from typing import Final

_log = logging.getLogger(__name__)

INLINE_MARKER: Final[str] = "<!-- ai-pr-review-inline -->"
# Bitbucket's comment renderer HTML-escapes raw `<!-- -->` comments instead of
# hiding them (#699), so its call sites use this reference-link-definition
# form instead — a no-op link definition that GitHub, GitLab, and Bitbucket
# all render as nothing.
INLINE_MARKER_HIDDEN: Final[str] = "[//]: # (ai-pr-review-inline)"
SUMMARY_MARKER_PREFIX: Final[str] = "<!-- ai-pr-review-summary"
SUMMARY_MARKER_HIDDEN_PREFIX: Final[str] = "[//]: # (ai-pr-review-summary"
ID_MAP_MARKER_PREFIX: Final[str] = "<!-- ai-pr-review-id-map:"
# Skip comments get their own marker so _list_skip_comments() can find them
# independently of _list_summary_comments(). INLINE_MARKER is also appended
# (for backward-compat stale-cleanup), but SKIP_MARKER is the upsert anchor.
SKIP_MARKER: Final[str] = "<!-- ai-pr-review-skip -->"
SKIP_MARKER_HIDDEN: Final[str] = "[//]: # (ai-pr-review-skip)"

_SHA_PATTERN = re.compile(r"\A[0-9a-f]{7,40}\Z")

# Matches a summary marker with optional sha= field, in either delimiter
# style, e.g.:
#   <!-- ai-pr-review-summary -->
#   <!-- ai-pr-review-summary sha=abc1234 -->
#   [//]: # (ai-pr-review-summary)
#   [//]: # (ai-pr-review-summary sha=abc1234)
# Each delimiter style is a fully self-contained alternative (own open+close,
# own sha group) rather than mixing-and-matching open/close independently —
# that would let a malformed body like "<!-- ai-pr-review-summary)" match.
# [ \t] (not \s) keeps a match from spanning multiple lines.
_SUMMARY_MARKER_RE = re.compile(
    r"<!--[ \t]*ai-pr-review-summary(?:[ \t]+sha=(?P<sha>[0-9a-f]+))?[ \t]*-->"
    r"|\[//\]:[ \t]*#[ \t]*\(ai-pr-review-summary(?:[ \t]+sha=(?P<sha2>[0-9a-f]+))?[ \t]*\)"
)


def _is_valid_sha(sha: str) -> bool:
    return bool(_SHA_PATTERN.match(sha))


def build_summary_marker(head_sha: str, *, hidden: bool = False) -> str:
    """Produce the summary marker, embedding head_sha when valid.

    Pass ``hidden=True`` (Bitbucket) to emit the reference-link-definition
    form, which Bitbucket's renderer actually hides — see INLINE_MARKER_HIDDEN.
    """
    if hidden:
        if _is_valid_sha(head_sha):
            return f"[//]: # (ai-pr-review-summary sha={head_sha})"
        return "[//]: # (ai-pr-review-summary)"
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
    sha = match.group("sha") or match.group("sha2")
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
    """Case-sensitive check for the inline ownership marker (either form)."""
    return INLINE_MARKER in body or INLINE_MARKER_HIDDEN in body


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
        _log.warning(
            "ai-pr-review: id-map marker present but unparseable: %s — raw: %.200s",
            exc,
            match.group(1),
        )
    return {}


def _hidden_marker_separator(body: str) -> str:
    """Separator to append a `[//]: # (...)` reference-link marker after.

    Per CommonMark, a link reference definition cannot interrupt a paragraph
    — appended right after a paragraph line with a single newline, it
    becomes a lazy continuation line and renders as literal text (unlike an
    HTML comment, which has no such restriction). It needs a preceding blank
    line. Bitbucket's own renderer does not enforce this rule (verified: it
    hides the marker either way), but this keeps the hidden-marker form
    correct for any CommonMark-compliant renderer that might reuse it.
    """
    if body.endswith("\n\n") or not body:
        return ""
    if body.endswith("\n"):
        return "\n"
    return "\n\n"


def append_inline_marker(body: str, *, marker: str = INLINE_MARKER) -> str:
    """Append an inline marker to body (idempotent re: either marker form).

    Pass ``marker=INLINE_MARKER_HIDDEN`` (Bitbucket) to append the hidden
    (reference-link) form instead of the default HTML-comment form.
    """
    if has_inline_marker(body):
        return body
    if not body:
        return marker
    if marker.startswith("[//]"):
        separator = _hidden_marker_separator(body)
    else:
        separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}{marker}"


def has_skip_marker(body: str) -> bool:
    """Case-sensitive check for the skip ownership marker (either form)."""
    return SKIP_MARKER in body or SKIP_MARKER_HIDDEN in body


def append_skip_marker(
    body: str,
    *,
    inline_marker: str = INLINE_MARKER,
    skip_marker: str = SKIP_MARKER,
) -> str:
    """Append a skip marker (and an inline marker) to body (idempotent).

    Both markers are appended so that:
    - SKIP_MARKER serves as the upsert anchor for _list_skip_comments().
    - INLINE_MARKER preserves backward-compat with the stale-cleanup path
      which gates on INLINE_MARKER.

    Pass ``inline_marker=INLINE_MARKER_HIDDEN, skip_marker=SKIP_MARKER_HIDDEN``
    (Bitbucket) to append the hidden (reference-link) forms instead.
    """
    if not has_inline_marker(body):
        if inline_marker.startswith("[//]"):
            separator = _hidden_marker_separator(body)
        else:
            separator = "" if body.endswith("\n") else "\n"
        body = f"{body}{separator}{inline_marker}"
    if not has_skip_marker(body):
        body = f"{body}\n{skip_marker}"
    return body


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
    match = _SUMMARY_MARKER_RE.search(body)
    if not match:
        hint = context_hint or body[:80].replace("\n", " ")
        print(
            f"WARNING: replace_summary_sha called on body with no summary marker "
            f"({hint!r}); returning body unchanged",
            file=sys.stderr,
        )
        return body
    # Preserve whichever delimiter style is already in the body (HTML-comment
    # vs. the hidden reference-link form) rather than forcing a conversion.
    # The two alternatives are now fully self-contained (own open+close), so
    # checking the matched text's own prefix is exact.
    if match.group(0).startswith("[//]"):
        replacement = f"[//]: # (ai-pr-review-summary sha={new_sha})"
    else:
        replacement = f"<!-- ai-pr-review-summary sha={new_sha} -->"
    return _SUMMARY_MARKER_RE.sub(replacement, body, count=1)
