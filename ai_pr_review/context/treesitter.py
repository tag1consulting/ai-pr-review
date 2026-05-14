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
        from tree_sitter_language_pack import get_parser  # type: ignore[import]
    except ImportError:
        logger.warning(
            "tree-sitter-language-pack not installed; context enrichment disabled. "
            "Install with: pip install 'ai-pr-review[context]'"
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

    try:
        tree = parser.parse(src.encode())
    except Exception as exc:
        logger.warning("tree-sitter: parse error for language %r: %s", language, exc)
        return []

    refs: list[SymbolRef] = []
    seen: set[str] = set()

    def _walk(node: object) -> None:  # type: ignore[misc]
        node_type: str = getattr(node, "type", "")
        if node_type in _IDENTIFIER_NODE_TYPES:
            name: str = getattr(node, "text", b"").decode(errors="replace")
            if (
                len(name) >= _MIN_SYMBOL_LEN
                and name not in _STOP_WORDS
                and name.isidentifier()
                and name not in seen
            ):
                seen.add(name)
                # Map tree-sitter's 0-based row to hunk line (1-based)
                ts_row: int = getattr(getattr(node, "start_point", None), "row", 0) if hasattr(node, "start_point") else 0
                hunk_line = line_map.get(ts_row, ts_row + 1)
                refs.append(SymbolRef(name=name, kind=node_type, line=hunk_line))
        for child in getattr(node, "children", []):
            _walk(child)

    _walk(tree.root_node)
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
