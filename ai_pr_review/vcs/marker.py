"""Ownership marker for VCS comments — resolves #183, #184.

The inline marker gates stale-cleanup. The GitHub and GitLab provider
implementations MUST only resolve/dismiss comments whose body contains
INLINE_MARKER, protecting other bots' reviews and threads from being touched
by our cleanup paths.

The summary marker format matches the bash engine so comments posted by bash
are still recognized by the Python engine.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
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
# Precedes the token-usage payload (#758) in every `token-usage-display` mode
# — `full` (the `<details>` accordion), `compact` (the one-line summary), and
# `off` (nothing but this marker). A stable, mode-independent anchor is the
# point: GitLab's summary-note upsert, Bitbucket's incremental
# walkthrough-boundary extraction, and both prior-body finding scanners
# (vcs/_finding_ids.py, slash/dismiss.py) all need to find "where the usage
# block starts/ends" without caring which mode produced the body they are
# re-reading. Before this, GitLab and Bitbucket anchored on
# TOKEN_TABLE_OPEN_MARKER (the accordion's own opening tag) below, which only
# exists in `full` mode.
USAGE_MARKER: Final[str] = "<!-- ai-pr-review-usage -->"
USAGE_MARKER_HIDDEN: Final[str] = "[//]: # (ai-pr-review-usage)"

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

# Bitbucket-only hidden form (#699: Bitbucket's renderer shows raw `<!-- -->`
# comments as literal text instead of hiding them). The payload is base64
# rather than raw JSON: a fingerprint embeds the finding's file path
# verbatim (`_finding_ids.fingerprint`), and a path containing `)` would
# otherwise prematurely close the `[//]: # (...)` reference-link definition,
# corrupting the marker and leaking JSON fragments into the visible comment.
ID_MAP_MARKER_HIDDEN_PREFIX: Final[str] = "[//]: # (ai-pr-review-id-map:"
_ID_MAP_MARKER_HIDDEN_RE = re.compile(
    r"\[//\]:[ \t]*#[ \t]*\(ai-pr-review-id-map:([A-Za-z0-9+/=]+)\)"
)


def build_id_map_marker(id_map: dict[str, int], *, hidden: bool = False) -> str:
    """Produce a marker embedding the finding ID map.

    The marker is machine-readable and (in the default form) invisible to
    users.  It is embedded in the review body so the ID map can be
    reconstructed from a single REST call to list reviews — no per-thread
    fetching required.

    Default format: ``<!-- ai-pr-review-id-map: {"<fingerprint>": <id>, ...} -->``

    Pass ``hidden=True`` (Bitbucket) to emit the reference-link-definition
    form instead, with the JSON payload base64-encoded (see module-level note
    above for why raw JSON isn't safe to embed there).
    """
    payload = json.dumps(id_map, separators=(",", ":"), sort_keys=True)
    if hidden:
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        return f"{ID_MAP_MARKER_HIDDEN_PREFIX}{encoded})"
    return f"<!-- ai-pr-review-id-map: {payload} -->"


def extract_id_map(body: str) -> dict[str, int]:
    """Extract the finding ID map from a review body.

    Checks both marker forms (default HTML-comment, and the Bitbucket-only
    hidden/base64 form — see ``build_id_map_marker``). Returns an empty dict
    when no marker is present. Logs a warning and returns an empty dict when
    a marker is present but its payload is unparseable, so callers can
    distinguish "no marker" from "corrupt marker" via the log.

    Accepts both integer and whole-number float JSON values (e.g. ``1.0``)
    to tolerate serializer rounding.
    """
    payload_raw: str | None = None
    match = _ID_MAP_MARKER_RE.search(body)
    if match:
        payload_raw = match.group(1)
    else:
        hidden_match = _ID_MAP_MARKER_HIDDEN_RE.search(body)
        if hidden_match:
            try:
                payload_raw = base64.b64decode(
                    hidden_match.group(1), validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                _log.warning(
                    "ai-pr-review: hidden id-map marker present but "
                    "undecodable: %s", exc,
                )
                return {}

    if payload_raw is None:
        return {}
    try:
        data = json.loads(payload_raw)
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
            payload_raw,
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


def build_usage_block(payload: str, *, hidden: bool = False) -> str:
    """Prefix a rendered token-usage payload with the mode-independent marker.

    `payload` is whatever `review/reporting.py` rendered for the configured
    `token-usage-display` mode: the full `<details>` accordion (`full`), the
    compact one-line summary (`compact`), or `""` (`off`). The marker
    precedes the payload in every mode — including `off`, where it is the
    only thing emitted — so downstream consumers (see USAGE_MARKER's
    docstring above) always have the same anchor to search for regardless of
    which mode produced the body.

    Pass `hidden=True` (Bitbucket) for the reference-link-definition form.
    """
    marker = USAGE_MARKER_HIDDEN if hidden else USAGE_MARKER
    if not payload:
        return marker
    return f"{marker}\n\n{payload}"


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


# GitHub-only (this mechanism has no GitLab/Bitbucket counterpart yet — see
# issue #710). Records, per finding fingerprint, which durable verdict a
# human gave it via a slash command: "dismissed" (dismiss/false-positive/
# wont-fix — suppress forever) or "fixed" (recur-check: reappearing with the
# exact same fingerprint is worth re-surfacing, per the PR-comment-clutter
# design). This can't be read back from GraphQL's `isResolved` state: that
# flag is set identically by all four commands AND by resolve_stale's
# routine per-cycle cleanup sweep, so by the next review cycle it no longer
# distinguishes "resolved because a human said so" from "resolved because a
# human said this specific thing" from "resolved as part of unrelated
# housekeeping." Embedded in whichever review is currently canonical,
# parallel to ID_MAP_MARKER_PREFIX; written by `ai_pr_review.slash.dismiss`
# at command-handling time via `GitHubProvider.update_review_body`.
VERDICTS_MARKER_PREFIX: Final[str] = "<!-- ai-pr-review-verdicts:"
_VERDICTS_MARKER_RE = re.compile(r"<!-- ai-pr-review-verdicts: (\{[^}]*\}) -->")
# "recurred" is a tombstone, not a third human verdict: written in place of
# deleting a "fixed" entry when that finding reappears unchanged. Because
# merge_verdicts (ai_pr_review/vcs/_canonical.py) unions verdicts across
# every prior bot review body rather than trusting only the newest one (the
# newest review is not guaranteed to carry every verdict -- see
# _record_verdict's union-seeding fix), a bare delete on the newest body
# would not remove the key from an older body still in that union, and every
# subsequent run would re-classify the finding as recurred forever. Recording
# "recurred" instead is a real, retained value that permanently overrides the
# stale "fixed" entry once it has been seen once. classify() treats it
# identically to "no verdict at all" (falls through to normal open/new
# classification).
_VALID_VERDICTS: Final[frozenset[str]] = frozenset({"dismissed", "fixed", "recurred"})


def build_verdicts_marker(verdicts: dict[str, str]) -> str:
    """Produce a hidden HTML comment embedding the fingerprint -> verdict map.

    Format: ``<!-- ai-pr-review-verdicts: {"<fingerprint>": "dismissed"|"fixed", ...} -->``
    """
    payload = json.dumps(verdicts, separators=(",", ":"), sort_keys=True)
    return f"{VERDICTS_MARKER_PREFIX} {payload} -->"


def extract_verdicts(body: str) -> dict[str, str]:
    """Extract the fingerprint -> verdict map from a review body.

    Returns an empty dict when no marker is present. Logs a warning and
    returns an empty dict when a marker is present but its JSON is
    malformed. Entries whose value is not one of the known verdict strings
    are dropped individually (forward-compatible with a future verdict type
    this version doesn't recognize, without discarding the whole map).
    """
    match = _VERDICTS_MARKER_RE.search(body)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning(
            "ai-pr-review: verdicts marker present but unparseable: %s — raw: %.200s",
            exc,
            match.group(1),
        )
        return {}
    if not isinstance(data, dict):
        _log.warning(
            "ai-pr-review: verdicts marker present but not a JSON object (got %s) — raw: %.200s",
            type(data).__name__,
            match.group(1),
        )
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v) in _VALID_VERDICTS}


def upsert_verdicts_marker(body: str, verdicts: dict[str, str]) -> str:
    """Return `body` with its verdicts marker replaced, or appended if the
    body doesn't have one yet, carrying `verdicts`.

    Unlike the id-map marker (always freshly appended during a full
    body re-render in `post_findings`), this marker is surgically patched
    into an otherwise-unchanged review body by the slash-command dismiss
    path — so it needs an explicit replace-or-append, not just an append.
    """
    new_marker = build_verdicts_marker(verdicts)
    if _VERDICTS_MARKER_RE.search(body):
        return _VERDICTS_MARKER_RE.sub(new_marker, body, count=1)
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}{new_marker}"


# Per-inline-comment metadata marker (canonical-review reuse, read side).
# Embeds a finding's exact fingerprint(), category, and severity directly on
# its own inline comment so a later run can classify against it (exact
# recurrence check, cross-run fuzzy match, escalation detection) without
# reconstructing any of the three from rendered text. None of the three is
# otherwise recoverable: category is never rendered anywhere in a comment
# body, and severity is only recoverable by re-parsing the `**[Sev]**` token
# in the comment's first line (ai_pr_review.slash.dismiss.parse_inline_comment_header
# already isolates it as a throwaway group).
#
# Payload is base64-encoded, not raw JSON like VERDICTS_MARKER_PREFIX, because
# an inline comment's own body embeds `suggested_code` (a Finding field)
# UNESCAPED ahead of this marker (see github.py's _build_inline_comment_body):
# `is_suggestion_safe` only rejects triple backticks, so nothing stops a
# hostile finding's suggested code from containing text that looks like
# `<!-- ai-pr-review-finding: {...} -->`. A raw-JSON marker could then be
# forged or duplicated inside the rendered code fence, and a plain
# `.search()` can't tell a real marker from one sitting inside markdown a
# code fence just happens to render literally. Base64 closes that off the
# same way ID_MAP_MARKER_HIDDEN already does, and extract_inline_meta below
# additionally takes the LAST match in the body (the renderer always appends
# the real marker after everything else, including any suggestion fence) and
# validates the decoded severity/category against the known enums before
# trusting either.
INLINE_META_MARKER_PREFIX: Final[str] = "<!-- ai-pr-review-finding:"
_INLINE_META_MARKER_RE = re.compile(r"<!-- ai-pr-review-finding:([A-Za-z0-9+/=]+) -->")

# Cap on how many superseded fingerprints one comment's marker carries
# forward (see `prior_fingerprints` below). Bounds the marker's growth
# against a pathological case where a finding gets reworded on every single
# run forever; far larger than any real drift chain needs (#720's fix only
# ever needs the fingerprint recorded by the most recent human verdict to
# still be reachable).
_MAX_PRIOR_FINGERPRINTS: Final[int] = 20


@dataclass(frozen=True)
class InlineMeta:
    """Decoded payload of an inline finding's metadata marker.

    `cat`/`sev` are `None` when absent or when the decoded value doesn't
    match a currently-known `Category`/`Severity` literal — callers must
    treat `None` as "unrecoverable" (wildcard-compatible for category, "do
    not treat as a severity waiver" for severity), never as a real enum
    member.

    `prior_fps` (#720 fix) carries every fingerprint this same comment has
    ever been rendered with, oldest first, capped at
    `_MAX_PRIOR_FINGERPRINTS`. `github.py`'s `_apply_thread_update` PATCHes a
    fuzzy-matched "update"/"escalate" thread's comment in place, which
    changes `fp` to the new finding's exact fingerprint while the visible
    `**[F<n>]**` token stays the same, so a human who dismisses that thread
    based on the still-unchanged F-id can end up with a verdict recorded
    against whichever fingerprint was current *at dismiss time*, which may
    no longer be `fp` by the time this thread is read again.
    `ai_pr_review.vcs._canonical._find_thread_by_fingerprint` checks `fp` and
    `prior_fps` together so the thread stays discoverable by any fingerprint
    it has ever carried, not just its current one.
    """

    fp: str
    cat: str | None
    sev: str | None
    prior_fps: tuple[str, ...] = ()


def _valid_categories() -> frozenset[str]:
    from ai_pr_review.findings.models import CATEGORIES

    return frozenset(CATEGORIES)


def _valid_severities() -> frozenset[str]:
    from typing import get_args

    from ai_pr_review.findings.models import Severity

    return frozenset(get_args(Severity))


def build_inline_meta_marker(
    *,
    fingerprint: str,
    category: str,
    severity: str,
    prior_fingerprints: Sequence[str] = (),
) -> str:
    """Produce the base64-encoded per-comment metadata marker.

    `fingerprint` is `_finding_ids.fingerprint(f)`'s output verbatim
    (embeds the finding's file path, which may contain characters unsafe in
    an unescaped HTML comment — another reason this is base64, not raw JSON).

    `prior_fingerprints` (#720 fix): every fingerprint this comment has
    previously been rendered with, oldest first. Deduplicated and truncated
    to the last `_MAX_PRIOR_FINGERPRINTS` entries (dropping the oldest) so a
    finding reworded on every single run can't grow the marker without
    bound. Omitted from the payload entirely when empty, matching the
    pre-#720 marker shape exactly (no behavior change for a thread that has
    never been through the fuzzy "update"/"escalate" path).
    """
    payload: dict[str, object] = {"fp": fingerprint, "cat": category, "sev": severity}
    if prior_fingerprints:
        deduped = list(dict.fromkeys(prior_fingerprints))
        payload["pfp"] = deduped[-_MAX_PRIOR_FINGERPRINTS:]
    encoded_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded = base64.b64encode(encoded_payload.encode("utf-8")).decode("ascii")
    return f"{INLINE_META_MARKER_PREFIX}{encoded} -->"


def extract_inline_meta(body: str) -> InlineMeta | None:
    """Extract and validate the metadata marker from an inline comment body.

    Returns `None` when no marker is present, the payload doesn't decode, or
    it decodes to something without a usable `fp`. `cat`/`sev` are
    individually dropped to `None` (not treated as a reason to discard the
    whole result) when unrecognized, so a future new category/severity value
    degrades to "unrecoverable" for that one field rather than losing the
    fingerprint too.
    """
    matches = list(_INLINE_META_MARKER_RE.finditer(body))
    if not matches:
        return None
    # Last match: see the module-level comment above this section — a forged
    # marker-shaped string earlier in the body (e.g. inside a suggestion
    # fence) cannot appear after the real one.
    match = matches[-1]
    try:
        payload_raw = base64.b64decode(match.group(1), validate=True).decode("utf-8")
        data = json.loads(payload_raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.warning(
            "ai-pr-review: inline finding-meta marker present but undecodable: %s", exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    fp = data.get("fp")
    if not isinstance(fp, str) or not fp:
        return None
    cat = data.get("cat")
    cat = cat if isinstance(cat, str) and cat in _valid_categories() else None
    sev = data.get("sev")
    sev = sev if isinstance(sev, str) and sev in _valid_severities() else None
    pfp_raw = data.get("pfp")
    prior_fps = (
        tuple(x for x in pfp_raw if isinstance(x, str) and x)
        if isinstance(pfp_raw, list)
        else ()
    )
    return InlineMeta(fp=fp, cat=cat, sev=sev, prior_fps=prior_fps)
