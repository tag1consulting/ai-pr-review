"""Shared text-safety helpers for untrusted text that goes into a prompt.

Two layers apply to every untrusted embed:

1. Strip invisible characters. ``clean_text`` is the full rule for metadata and
   non-code fields. ``clean_code`` is a lighter rule for code, which keeps the
   joiners and direction marks that real source text can hold.
2. Escape closing tags (``escape_closing_tags``) so untrusted text cannot end
   the block that wraps it and forge a new block after it.

Always strip first and escape second, because an invisible character can hide a
tag from the escape pattern. Both layers are idempotent.
"""

from __future__ import annotations

import re
import unicodedata

_STRIP = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\U000e0000-\U000e007f]")

# Controls, the tag block, and the bidi override and isolate characters.
# ZWJ, ZWNJ, LRM and RLM stay, because emoji sequences and some scripts need them.
_CODE_STRIP = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\U000e0000-\U000e007f‪-‮⁦-⁩]")

# Closers of the blocks the engine builds around untrusted text.
BLOCK_TAGS = ("pr-context", "symbol-context", "repo-feedback")


def clean_text(text: str) -> str:
    """Drop control characters and every invisible format character (zero-width, direction marks, tag
    characters). Tab and newline stay. The same rule decides which paths can be index keys."""
    return "".join(c for c in _STRIP.sub("", text) if unicodedata.category(c) != "Cf")


def clean_code(text: str) -> str:
    """Light variant of ``clean_text`` for code. Drop controls, tag characters and bidi overrides.
    Keep ZWJ, ZWNJ, LRM and RLM. Tab, newline and carriage return stay."""
    return _CODE_STRIP.sub("", text)


def _closing_pattern(tags: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?i)<\s*/\s*(" + "|".join(re.escape(t) for t in tags) + ")")


_BLOCK_CLOSER = _closing_pattern(BLOCK_TAGS)


def escape_closing_tags(text: str, tags: tuple[str, ...] = BLOCK_TAGS) -> str:
    """Replace the start of a closing tag (case-insensitive, space-tolerant) with ``&lt;/tag``."""
    pattern = _BLOCK_CLOSER if tags == BLOCK_TAGS else _closing_pattern(tags)
    return pattern.sub(r"&lt;/\1", text)


def safe_text(text: str) -> str:
    """Full strip, then escape. Use for metadata and non-code fields."""
    return escape_closing_tags(clean_text(text))


# Categories that render as nothing or attach to a neighbor: format characters, combining marks and
# controls. A closer can hide inside them, so match on a copy with them removed.
_HIDING_CATEGORIES = frozenset({"Cf", "Mn", "Me", "Cc"})


def safe_code(text: str) -> str:
    """Light strip, then escape. Use for the diff and for code snippets.

    The light strip keeps ZWJ and ZWNJ, so a closer such as ``<ZWJ/pr-context>`` can survive it.
    Find closers on a copy with every format character, combining mark and control removed,
    then escape those spans in the original. Text outside a closer is left as it was, so
    legitimate emoji sequences stay intact.
    """
    text = clean_code(text)
    kept: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if unicodedata.category(ch) in _HIDING_CATEGORIES and ch not in "\t\n\r":
            continue
        kept.append(ch)
        origin.append(i)
    view = "".join(kept)
    matches = list(_BLOCK_CLOSER.finditer(view))
    if not matches:
        return text
    out: list[str] = []
    cursor = 0
    for m in matches:
        start = origin[m.start()]
        end = origin[m.end() - 1] + 1
        out.append(text[cursor:start])
        out.append("&lt;/" + m.group(1))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def code_fence(snippet: str) -> str:
    """Return a backtick fence longer than any backtick run in ``snippet`` (three at least)."""
    longest = max((len(m) for m in re.findall(r"`+", snippet)), default=0)
    return "`" * max(3, longest + 1)
