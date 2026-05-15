"""Tree-sitter symbol-reference extraction — E3.S1.

Extracts symbol references from a unified-diff hunk using tree-sitter grammars.
Requires the optional ``tree-sitter-language-pack`` package (install with
``pip install 'ai-pr-review[context]'``).  When the package or a specific
grammar is unavailable, the function logs a WARNING and returns an empty list
so the review continues unaffected.

Gated by ``AI_CONTEXT_ENRICHMENT=1`` — callers must check the flag before
invoking; this module does not re-check it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language → grammar name mapping
# ---------------------------------------------------------------------------

# Maps the language key used by ai_pr_review/agents/gates.py (and file
# extension detection) to the tree-sitter-language-pack grammar name.
_LANG_TO_GRAMMAR: dict[str, str] = {
    "python": "python",
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
    "go": "go",
    "php": "php",
    "ruby": "ruby",
    "rust": "rust",
    "bash": "bash",
    "shell": "bash",
    "java": "java",
    "cpp": "cpp",
    "c": "c",
}

# Node types considered "symbol references" per language.
_IDENTIFIER_NODE_TYPES: frozenset[str] = frozenset(
    {
        "identifier",
        "type_identifier",
        "field_identifier",
        "property_identifier",
        "attribute",
        "name",
        "dotted_name",
        "qualified_identifier",
        "namespace_identifier",
    }
)

# Minimum length to avoid noise from single-letter variables.
_MIN_SYMBOL_LEN = 2
# Common noise words to skip.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "if", "else", "for", "while", "return", "true", "false", "null",
        "nil", "none", "self", "this", "super", "new", "delete", "typeof",
        "instanceof", "import", "from", "as", "in", "and", "or", "not",
        "def", "class", "func", "fn", "let", "var", "const", "type",
        "interface", "struct", "enum", "pub", "priv", "mut", "async",
        "await", "yield", "raise", "throw", "try", "catch", "except",
        "finally", "with", "pass", "break", "continue", "print",
    }
)


@dataclass(frozen=True)
class SymbolRef:
    """A symbol reference found in a diff hunk."""

    name: str
    kind: str  # node type from tree-sitter
    line: int  # 1-based line number within the hunk


def _strip_diff_markers(hunk: str) -> tuple[str, dict[int, int]]:
    """Remove +/-/space diff markers; return clean source + hunk-line→src-line map."""
    src_lines: list[str] = []
    line_map: dict[int, int] = {}  # src_line_index → hunk_line_index (1-based)
    for hunk_lineno, raw_line in enumerate(hunk.splitlines(), start=1):
        if raw_line.startswith("@@") or raw_line.startswith("---") or raw_line.startswith("+++"):
            continue
        if raw_line.startswith("+") or raw_line.startswith(" "):
            src_idx = len(src_lines)
            line_map[src_idx] = hunk_lineno
            src_lines.append(raw_line[1:])
        # Lines starting with '-' are removed; skip them (not in new file).
    return "\n".join(src_lines), line_map


def extract_symbol_refs(diff_hunk: str, language: str) -> list[SymbolRef]:
    """Extract symbol references from *diff_hunk* for the given *language*.

    Returns an empty list when:
    - ``tree-sitter-language-pack`` is not installed
    - No grammar is registered for *language*
    - Parsing fails for any reason

    All failures are fail-soft: a WARNING is logged and ``[]`` returned.
    """
    if not diff_hunk.strip():
        return []

    grammar_name = _LANG_TO_GRAMMAR.get(language.lower())
    if grammar_name is None:
        return []

    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        # Include the cause in BOTH the message body (visible in JSON loggers
        # / truncating aggregators) and via exc_info (full chain incl. any
        # __cause__ from a broken native extension).
        logger.warning(
            "tree-sitter-language-pack unavailable; context enrichment disabled. "
            "Install with: pip install 'ai-pr-review[context]'. Cause: %s",
            exc,
            exc_info=exc,
        )
        return []

    try:
        parser = get_parser(grammar_name)
    except Exception as exc:
        logger.warning("tree-sitter: could not load grammar %r: %s", grammar_name, exc)
        return []

    src, line_map = _strip_diff_markers(diff_hunk)
    if not src.strip():
        return []
    src_bytes = src.encode()

    # tree-sitter-language-pack 1.x parser accepts str (and exposes parse_bytes
    # for the bytes variant).  Earlier 0.x took bytes via parse().  Try the
    # current API first, fall back to the old one.
    try:
        tree = parser.parse(src)
    except (TypeError, AttributeError):
        try:
            tree = parser.parse(src_bytes)
        except Exception as exc:
            logger.warning(
                "tree-sitter: parse error for language %r: %s", language, exc,
            )
            return []
    except Exception as exc:
        logger.warning("tree-sitter: parse error for language %r: %s", language, exc)
        return []

    refs: list[SymbolRef] = []
    seen: set[str] = set()

    def _attr_or_call(obj: object, name: str, default: object) -> object:
        """Read an attribute that may be a property OR a method.

        tree-sitter-language-pack 1.x exposes everything as methods (.kind(),
        .byte_range(), .start_point()) while older releases used properties.
        Support both so we don't have to ship two implementations.
        """
        val = getattr(obj, name, default)
        if callable(val) and not isinstance(val, type):
            try:
                return val()
            except TypeError:
                return default
        return val

    def _node_text(node: object) -> str:
        """Extract identifier text from a node by slicing the source bytes.

        Supports three byte-range shapes across tree-sitter versions:
        - 1.x C-extension: ``ByteRange`` object with ``.start`` and ``.end``
        - 0.x Python: ``(start, end)`` tuple
        - direct ``.start_byte`` / ``.end_byte`` attrs (also 1.x fallback)
        """
        # Prefer direct start_byte/end_byte attrs (works on 1.x)
        sb = _attr_or_call(node, "start_byte", None)
        eb = _attr_or_call(node, "end_byte", None)
        if isinstance(sb, int) and isinstance(eb, int):
            return src_bytes[sb:eb].decode(errors="replace")

        br = _attr_or_call(node, "byte_range", None)
        if br is not None:
            br_start = _attr_or_call(br, "start", None)
            br_end = _attr_or_call(br, "end", None)
            if isinstance(br_start, int) and isinstance(br_end, int):
                return src_bytes[br_start:br_end].decode(errors="replace")
            if isinstance(br, tuple) and len(br) == 2:
                start, end = br
                if isinstance(start, int) and isinstance(end, int):
                    return src_bytes[start:end].decode(errors="replace")

        # Fallback to .text attribute (old API, bytes or str)
        text = _attr_or_call(node, "text", "")
        if isinstance(text, bytes):
            return text.decode(errors="replace")
        return str(text or "")

    def _walk(node: object) -> None:
        node_kind = _attr_or_call(node, "kind", None) or _attr_or_call(node, "type", "")
        if not isinstance(node_kind, str):
            return
        if node_kind in _IDENTIFIER_NODE_TYPES:
            name = _node_text(node)
            if (
                len(name) >= _MIN_SYMBOL_LEN
                and name not in _STOP_WORDS
                and name.isidentifier()
                and name not in seen
            ):
                seen.add(name)
                # Map tree-sitter's 0-based row to hunk line (1-based).
                # 1.x exposes start_position() -> Point(row, column).
                # 0.x exposes start_point as a property returning (row, column).
                sp = _attr_or_call(node, "start_position", None)
                if sp is None:
                    sp = _attr_or_call(node, "start_point", None)
                ts_row = 0
                if isinstance(sp, tuple) and sp and isinstance(sp[0], int):
                    ts_row = sp[0]
                elif sp is not None:
                    raw_row = _attr_or_call(sp, "row", 0)
                    if isinstance(raw_row, int):
                        ts_row = raw_row
                hunk_line = line_map.get(ts_row, ts_row + 1)
                refs.append(SymbolRef(name=name, kind=node_kind, line=hunk_line))
        # Iterate children: new API uses child(i)/child_count(); old uses .children
        child_count = _attr_or_call(node, "child_count", None)
        if isinstance(child_count, int):
            for i in range(child_count):
                try:
                    child = node.child(i)  # type: ignore[attr-defined]
                except (TypeError, AttributeError):
                    break
                if child is not None:
                    _walk(child)
        else:
            children = _attr_or_call(node, "children", []) or []
            if isinstance(children, (list, tuple)):
                for child in children:
                    _walk(child)

    root = _attr_or_call(tree, "root_node", None)
    if root is None:
        return []
    _walk(root)
    return refs


# ---------------------------------------------------------------------------
# Lightweight fallback: regex-based extraction when tree-sitter is unavailable
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{1,})\b")


def extract_symbol_refs_fallback(diff_hunk: str) -> list[SymbolRef]:
    """Regex fallback for symbol extraction when tree-sitter is unavailable.

    Less precise than tree-sitter — does not distinguish node types — but
    provides basic enrichment support for any language.  Not called by default;
    callers may invoke this when ``extract_symbol_refs`` returns empty due to a
    missing grammar.
    """
    refs: list[SymbolRef] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(diff_hunk.splitlines(), start=1):
        if raw.startswith(("@@", "---", "+++")):
            continue
        if not (raw.startswith("+") or raw.startswith(" ")):
            continue
        for m in _IDENTIFIER_RE.finditer(raw[1:]):
            name = m.group(1)
            if name not in _STOP_WORDS and name not in seen:
                seen.add(name)
                refs.append(SymbolRef(name=name, kind="identifier", line=lineno))
    return refs
