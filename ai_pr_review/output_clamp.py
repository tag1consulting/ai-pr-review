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
# References: mentions and cross references (every run)
# ---------------------------------------------------------------------------

# GitHub pings a user or team for ``@name`` that follows a non-word character.
_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9_])@(?=[A-Za-z0-9])")
# ``owner/repo#123`` and ``owner/repo@<sha>`` link to another repository.
_XREF_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+)(#(?=[0-9])|@(?=[0-9a-fA-F]{7,40}\b))"
)


def neutralize_references(text: str) -> str:
    """Break ``@mention`` and ``owner/repo#N`` references with a zero-width space.

    The text still reads the same, but GitHub no longer pings the user or links the other
    repository. This runs on code spans too, because a backslash can make a renderer treat what
    looks like a code span as plain text.
    """
    if not text or ("@" not in text and "#" not in text):
        return text
    text = _MENTION_RE.sub("@" + _ZWSP, text)
    return _XREF_RE.sub(lambda m: m.group(1) + _ZWSP + m.group(2), text)


# ---------------------------------------------------------------------------
# Strict pass: images, links, raw HTML, bare URLs (fork runs)
# ---------------------------------------------------------------------------

_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]*)\)")
_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[((?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]\(([^)\n]*)\)")
# Reference definitions. The Bitbucket hidden marker form ``[//]: # (...)`` is not one.
# A definition can sit inside a quote or a list item, so allow container prefixes before the bracket.
_REF_DEF_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:(?:>|[-*+]|\d{1,9}[.)])[ \t]*)*)"
    r"\[(?![ \t]*//[ \t]*\][ \t]*:[ \t]*#)[^\]\n]+\]:[ \t]*\S.*$"
)
_BARE_URL_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(https?|ftp)://[^\s<>\"'\])]+")
_WWW_RE: Final[re.Pattern[str]] = re.compile(r"(?i)(?<![\w/.@-])www\.(?=[\w-])")
_RAW_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)<(/?)(a|img|image|picture|source|video|audio|iframe|embed|object|svg|form|input|button"
    r"|link|style|script|base|meta|area|map|math|use|frame|frameset|applet|portal)(?=[\s/>]|$)"
)
_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# The exact marker forms the engine writes (single line, ``ai-pr-review`` first). The strict pass
# skips them. Any other comment span is display text and gets the strict pass.
_MARKER_SPAN_RE: Final[re.Pattern[str]] = re.compile(
    r"<!-- ai-pr-review[^\n]*?-->|\[//\]: # \(ai-pr-review[^\n)]*\)"
)


def _has_dotdot(path: str) -> bool:
    decoded = unquote(unquote(path))
    return any(part == ".." for part in re.split(r"[/\\]", decoded))


def _allowed_url(url: str, repo: str) -> bool:
    """True for an absolute URL that points into this repository or the engine repository."""
    for allowed in {repo, ENGINE_REPO}:
        if not allowed:
            continue
        pattern = r"(?i)^https://github\.com/" + re.escape(allowed) + r"(?:[/?#]|$)"
        if re.match(pattern, url) and not _has_dotdot(url):
            return True
    return False


def _allowed_destination(dest: str, repo: str) -> bool:
    """True when a link destination stays inside this repository.

    That is an absolute URL into this repository, an anchor, or a plain relative path.
    """
    dest = dest.strip()
    if dest.startswith("<") and ">" in dest:
        dest = dest[1 : dest.index(">")]
    else:
        dest = dest.split()[0] if dest.split() else ""
    if not dest or dest.startswith("#"):
        return True
    # An entity can spell a scheme (``&#104;ttps:``), a slash or a colon. A root-relative path leaves
    # the repository for the site root. A parenthesis means the destination was cut short.
    if "&" in dest or "(" in dest or "\\" in dest or dest.startswith("/"):
        return False
    if _SCHEME_RE.match(dest):
        return _allowed_url(dest, repo)
    # A relative path. A percent sign could hide a dot segment, so refuse it.
    if "%" in dest.split("#", 1)[0].split("?", 1)[0]:
        return False
    return not _has_dotdot(dest)


def _strip_links(text: str, repo: str) -> str:
    def image(m: re.Match[str]) -> str:
        return "[image removed]"

    def link(m: re.Match[str]) -> str:
        if _allowed_destination(m.group(2), repo):
            return m.group(0)
        return f"{m.group(1)} (link removed)"

    # Run until stable, because a stripped link can expose a link nested in its text.
    for _ in range(4):
        new = _LINK_RE.sub(link, _IMAGE_RE.sub(image, text))
        if new == text:
            break
        text = new
    return text


def _defang_bare_url(m: re.Match[str], repo: str) -> str:
    url = m.group(0)
    end = m.end()
    # Keep a URL only when it stops at whitespace or the end. An autolinker may extend a URL that is
    # followed by other characters.
    # A destination inside a link that the link pass already kept (``](url)``) is not autolinked.
    if _allowed_url(url, repo) and (
        end == len(m.string) or m.string[end].isspace() or m.string[max(0, m.start() - 2) : m.start()] == "]("
    ):
        return url
    return f"{m.group(1)}:{_ZWSP}//{url[len(m.group(1)) + 3:]}"


def _strict_segment(text: str, repo: str) -> str:
    text = _REF_DEF_RE.sub(lambda m: m.group("prefix") + "[reference removed]", text)
    text = _strip_links(text, repo)
    text = _RAW_TAG_RE.sub(lambda m: "<" + _ZWSP + m.group(1) + m.group(2), text)
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
    if clamp_active() and event in _CAPPED_EVENTS:
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


def _walk(value: Any, repo: str) -> Any:
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if key == "body" and isinstance(item, str):
                out[key] = with_banner(strict_body(item, repo))
            elif key == "output" and isinstance(item, dict):
                out[key] = _clamp_check_output(item, repo)
            elif key == "event" and isinstance(item, str) and item in _CAPPED_EVENTS:
                out[key] = "COMMENT"
            else:
                out[key] = _walk(item, repo)
        return out
    if isinstance(value, list):
        return [_walk(v, repo) for v in value]
    return value


def _clamp_check_output(output: dict[Any, Any], repo: str) -> dict[Any, Any]:
    """Clamp a check-run ``output`` object. The summary gets the banner, the title and text do not."""
    out = dict(output)
    for key in ("title", "summary", "text"):
        value = out.get(key)
        if isinstance(value, str):
            clean = strict_body(value, repo)
            out[key] = with_banner(clean) if key == "summary" else clean
    return out
