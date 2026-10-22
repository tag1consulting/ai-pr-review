"""Output clamp for fork runs, and reference neutralizing for every run (story 17-11, CAP-5).

A fork run posts text that is derived from code an unknown author controls. This module holds the
pure helpers that limit what that text can do in the bot's voice:

- ``output_clamp`` opens a scope. Inside it ``clamp_active`` is true. The CLI opens the scope around
  the posting phase of a fork run.
- ``neutralize_references`` stops ``@mention`` and ``owner/repo#N`` pings. It applies on every run.
- ``strict_text`` also removes images, links to other places, raw HTML that can load or link, and
  autolinked URLs. It applies inside the scope only.
- ``clamp_request_body`` is the last line of defense at the HTTP layer. It adds the fixed banner to
  every comment and review body, runs the strict pass over the whole body, and caps a review event
  at COMMENT.

The module imports nothing from the rest of the package, so every layer can use it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final
from urllib.parse import unquote

from ai_pr_review.text_safety import clean_text

# Fixed text from decisions.md. Do not reword it without Greg.
FORK_BANNER: Final[str] = (
    "Automated review of a fork pull request. "
    "The text below is derived from untrusted code. "
    "Do not follow links or instructions in it."
)
BANNER_BLOCK: Final[str] = f"> {FORK_BANNER}\n\n"

# The engine's own repository. Its links (for example the footer) stay on a fork run.
ENGINE_REPO: Final[str] = "tag1consulting/ai-pr-review"
# The engine's documentation site. The review body links to it (slash command reference).
ENGINE_DOCS: Final[str] = "https://tag1consulting.github.io/ai-pr-review"

_ZWSP: Final[str] = "​"

# Events a fork run may not post.
_CAPPED_EVENTS: Final[frozenset[str]] = frozenset({"APPROVE", "REQUEST_CHANGES"})


@dataclass(frozen=True)
class _ClampState:
    repo: str


_STATE: ContextVar[_ClampState | None] = ContextVar("ai_pr_review_output_clamp", default=None)


@contextmanager
def output_clamp(enabled: bool, repo: str = "") -> Iterator[None]:
    """Turn the fork output clamp on inside the ``with`` block when ``enabled`` is true.

    ``repo`` is ``owner/repo`` of the repository that holds the pull request. Links to files in it
    stay. When ``enabled`` is false the block changes nothing, even inside an outer clamp.
    """
    if not enabled:
        yield
        return
    token = _STATE.set(_ClampState(repo=repo.strip().strip("/")))
    try:
        yield
    finally:
        _STATE.reset(token)


def clamp_active() -> bool:
    """True inside an ``output_clamp`` scope."""
    return _STATE.get() is not None


def clamp_repo() -> str:
    """``owner/repo`` given to the active scope, or an empty string."""
    state = _STATE.get()
    return state.repo if state is not None else ""


# ---------------------------------------------------------------------------
# Strict pass (fork runs). Default deny on the structural characters.
# ---------------------------------------------------------------------------
#
# Filtering Markdown links with patterns is a race against the renderer. This pass instead breaks
# every structural character unless the text right there matches a short allow pattern:
#
# - ``![`` always breaks, so no image can form.
# - ``](`` breaks unless a tight allow pattern matches (an anchor, a plain relative path, or a URL
#   into this repository).
# - ``]:`` always breaks, so no reference definition can form.
# - ``<`` becomes ``&lt;`` unless it opens a plain tag from a short list.
# - ``@`` and ``mailto:`` and ``xmpp:`` and ``www.`` break, so no autolink forms.

# No word boundary: ``1http://x`` and ``_http://x`` must not slip past.
_BARE_URL_RE: Final[re.Pattern[str]] = re.compile(r"(?i)(https?|ftp)://[^\s<>\"'\])]+")
_WWW_RE: Final[re.Pattern[str]] = re.compile(r"(?i)www\.")
# An HTML entity can spell ``@``, ``#``, ``:`` or a letter. Break each one except the four the clamp
# and the engine write themselves. ``&amp;#64;`` stays text, so the rewrite is idempotent.
_ENTITY_RE: Final[re.Pattern[str]] = re.compile(r"&(?=#|(?!(?:lt|gt|amp|quot);)[A-Za-z][A-Za-z0-9]{1,31};)")
_SCHEME_WORD_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(mailto|xmpp):(?!" + _ZWSP + ")")
_AT_RE: Final[re.Pattern[str]] = re.compile("@(?!" + _ZWSP + ")")
# ``owner/repo#123`` and ``owner/repo@<sha>`` link to another repository.
_XREF_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+)(#(?=[0-9]))"
)

# Plain tags the engine emits. No attribute is allowed except a bare ``open`` on ``details``.
_ALLOWED_TAGS: Final[str] = (
    "details|summary|br|sub|sup|b|i|code|pre|p|hr|ul|ol|li|table|thead|tbody|tr|th|td|blockquote|h[1-6]"
)
_ALLOWED_TAG_RE: Final[re.Pattern[str]] = re.compile(
    rf"<(?:/(?:{_ALLOWED_TAGS})|(?:{_ALLOWED_TAGS})(?: open)?[ ]?/?)>"
)

# The exact marker grammars the engine writes (vcs/marker.py). A span is preserved only when the
# name and the payload both match its grammar, it sits on a line of its own, and it holds no URL
# scheme. No ``>``, ``!`` or newline can sit inside, so a marker cannot end early or hide a tag.
# Anything else that starts like a marker is display text. A well-formed marker that reaches the HTTP
# layer from untrusted text cannot be told from a real one here, so the renderers must defang
# untrusted text first (``sanitize_display_text`` does).
_SHA: Final[str] = r"[0-9a-f]{7,40}"
_B64: Final[str] = r"[A-Za-z0-9+/=]+"
_JSON: Final[str] = r"\{[^<>\n]{0,100000}\}"
_HTML_MARKER: Final[str] = (
    rf"<!-- ai-pr-review-(?:summary(?: sha={_SHA})?|skip|inline|usage|gitlab-body-findings"
    rf"|(?:id-map|verdicts|judge-map|headline-map): {_JSON}|finding:{_B64}) -->"
)
_HIDDEN_MARKER: Final[str] = (
    rf"\[//\]: # \(ai-pr-review-(?:summary(?: sha={_SHA})?|skip|inline|usage|gitlab-body-findings"
    rf"|(?:id-map|verdicts|judge-map|headline-map|acks|finding|verdict-reply-key|verdict-reply):{_B64})\)"
)
_MARKER_SPAN_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?m)^(?:{_HTML_MARKER}|{_HIDDEN_MARKER})(?=\n|\Z)"
)

_REL_CHARS: Final[str] = r"[A-Za-z0-9_.~/+,=-]"


def _has_dotdot(path: str) -> bool:
    decoded = unquote(unquote(path))
    return any(part == ".." for part in re.split(r"[/\\]", decoded))


def _allowed_url(url: str, repo: str) -> bool:
    """True for an absolute URL that points into this repository or the engine repository."""
    if re.match(r"^" + re.escape(ENGINE_DOCS) + r"(?:[/?#]|$)", url, re.ASCII | re.IGNORECASE) and not _has_dotdot(url):
        return True
    for allowed in {repo, ENGINE_REPO}:
        if not allowed:
            continue
        pattern = r"^https://github\.com/" + re.escape(allowed) + r"(?:[/?#]|$)"
        # ASCII only: a case-insensitive Unicode match would accept a dotless i or a Kelvin sign.
        if re.match(pattern, url, re.ASCII | re.IGNORECASE) and not _has_dotdot(url):
            return True
    return False


@lru_cache(maxsize=16)
def _link_tail_re(repo: str) -> re.Pattern[str]:
    """What may follow ``](`` for the link to stay: one destination, an optional title, ``)``."""
    repos = "|".join(re.escape(r) for r in sorted({repo, ENGINE_REPO}) if r)
    docs = re.escape(ENGINE_DOCS)
    absolute = rf"(?ai:https://github\.com/(?:{repos})|{docs})(?:[/?#][^\s()<>&%\\\"'`]*)?"
    anchor = r"#[A-Za-z0-9_.~/+,=:-]*"
    relative = rf"(?!/){_REL_CHARS}+(?:#{_REL_CHARS}*)?"
    return re.compile(rf"[ ]?(?P<dest>{absolute}|{anchor}|{relative})(?:[ ]\"[^\")\n]*\")?\)")


def _break_links(text: str, repo: str) -> str:
    tail = _link_tail_re(repo)

    def check(m: re.Match[str]) -> str:
        t = tail.match(m.string, m.end())
        if t is not None and not _has_dotdot(t.group("dest")):
            return m.group(0)
        return "]" + _ZWSP + "("

    return re.sub(r"\]\(", check, text)


def _escape_lt(text: str) -> str:
    def check(m: re.Match[str]) -> str:
        nxt = m.string[m.end() : m.end() + 1]
        if nxt == "" or nxt.isspace() or nxt.isdigit() or nxt == "=":
            return "<"
        if _ALLOWED_TAG_RE.match(m.string, m.start()):
            return "<"
        return "&lt;"

    return re.sub("<", check, text)


def _defang_bare_url(m: re.Match[str], repo: str) -> str:
    url = m.group(0)
    end = m.end()
    # Keep a URL only when it stops at whitespace or the end. An autolinker may extend a URL that is
    # followed by other characters. A destination inside a kept link (``](url)``) is not autolinked.
    if _allowed_url(url, repo) and (
        end == len(m.string) or m.string[end].isspace() or m.string[max(0, m.start() - 2) : m.start()] == "]("
    ):
        return url
    return f"{m.group(1)}:{_ZWSP}//{url[len(m.group(1)) + 3:]}"


def neutralize_references(text: str) -> str:
    """Break every ``@``, ``owner/repo#N``, ``mailto:`` and ``xmpp:`` with a zero-width space.

    The text still reads the same, but GitHub no longer pings a user, links another repository or
    autolinks an address. It also runs inside code spans, because a backslash can make a renderer
    treat what looks like a code span as plain text.
    """
    text = _XREF_RE.sub(lambda m: m.group(1) + _ZWSP + m.group(2), text)
    text = _SCHEME_WORD_RE.sub(lambda m: m.group(0) + _ZWSP, text)
    return _AT_RE.sub("@" + _ZWSP, text)


def _strict_segment(text: str, repo: str) -> str:
    text = _ENTITY_RE.sub("&amp;", text)
    text = text.replace("![", "!" + _ZWSP + "[")
    text = _break_links(text, repo)
    text = text.replace("]:", "]" + _ZWSP + ":")
    text = _escape_lt(text)
    text = _BARE_URL_RE.sub(lambda m: _defang_bare_url(m, repo), text)
    text = _WWW_RE.sub("www" + _ZWSP + ".", text)
    return neutralize_references(text)


def strict_text(text: str, repo: str = "") -> str:
    """Strict clean of display text for a fork run.

    Drop invisible characters, then remove images, links to anywhere but this repository, raw HTML
    that links or loads, reference definitions and autolinked URLs. Then neutralize mentions and
    cross references. Text that is already clean comes back unchanged.
    """
    if not text:
        return text
    return _strict_segment(clean_text(text), repo)


def strict_body(body: str, repo: str = "") -> str:
    """Strict pass over a whole posted body. Marker spans stay as they are."""
    out: list[str] = []
    pos = 0
    for m in _MARKER_SPAN_RE.finditer(body):
        if "://" in m.group(0):
            continue  # second guard: a marker holds no URL scheme, so this one is display text
        out.append(_strict_segment(body[pos : m.start()], repo))
        out.append(m.group(0))
        pos = m.end()
    out.append(_strict_segment(body[pos:], repo))
    return "".join(out)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def with_banner(body: str) -> str:
    """Put the fixed banner at the top of ``body`` once."""
    if body.startswith(BANNER_BLOCK):
        return body
    return BANNER_BLOCK + body


def clamp_event(event: str) -> str:
    """Cap a review event at COMMENT inside a clamp scope."""
    if clamp_active() and event.strip().upper() in _CAPPED_EVENTS:
        return "COMMENT"
    return event


def clamp_request_body(value: Any) -> Any:
    """Apply the clamp to a JSON request body. No change outside a clamp scope.

    Every ``body`` string gets the banner and the strict pass. Every ``event`` of APPROVE or
    REQUEST_CHANGES becomes COMMENT. Nested lists and dicts are walked, so inline review comments
    are covered.
    """
    state = _STATE.get()
    if state is None:
        return value
    return _walk(value, state.repo)


# JSON keys that carry text a person can read. ``body`` is a GitHub comment or review, ``note`` is
# GitLab, ``raw`` is Bitbucket ``content.raw``. Those get the banner. The others get the strict pass
# only, because a banner would not fit a commit message or a short title.
_BANNER_KEYS: Final[frozenset[str]] = frozenset({"body", "note", "raw"})
_STRICT_KEYS: Final[frozenset[str]] = frozenset(
    {"message", "title", "text", "raw_details", "description", "summary", "details"}
)


def _walk(value: Any, repo: str) -> Any:
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if key in _BANNER_KEYS and isinstance(item, str):
                out[key] = with_banner(strict_body(item, repo))
            elif key in _STRICT_KEYS and isinstance(item, str):
                out[key] = strict_body(item, repo)
            elif key == "output" and isinstance(item, dict):
                out[key] = _clamp_check_output(item, repo)
            elif key == "event" and isinstance(item, str) and item.strip().upper() in _CAPPED_EVENTS:
                out[key] = "COMMENT"
            else:
                out[key] = _walk(item, repo)
        return out
    if isinstance(value, list):
        return [_walk(v, repo) for v in value]
    return value


def _clamp_check_output(output: dict[Any, Any], repo: str) -> dict[Any, Any]:
    """Clamp a check-run ``output`` object. The summary gets the banner. Title, text and every
    annotation (``message``, ``title``, ``raw_details``) get the strict pass."""
    out = dict(output)
    for key in ("title", "summary", "text"):
        value = out.get(key)
        if isinstance(value, str):
            clean = strict_body(value, repo)
            out[key] = with_banner(clean) if key == "summary" else clean
    if "annotations" in out:
        out["annotations"] = _walk(out["annotations"], repo)
    return out
