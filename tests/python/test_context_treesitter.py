"""Tests for ai_pr_review.context.treesitter — E3.S1."""

from ai_pr_review.context.treesitter import (
    SymbolRef,
    _strip_diff_markers,
    extract_symbol_refs,
    extract_symbol_refs_fallback,
)

_PY_HUNK = """\
+def my_function(x, y):
+    result = helper(x)
+    return result + y
 # unchanged
-def old_function():
-    pass
"""


def test_strip_diff_markers_removes_markers() -> None:
    stripped, _linemap = _strip_diff_markers(_PY_HUNK)
    assert not any(line.startswith(("+", "-")) for line in stripped.splitlines())


def test_strip_diff_markers_keeps_context_lines() -> None:
    stripped, _linemap = _strip_diff_markers(_PY_HUNK)
    assert "# unchanged" in stripped


def test_fallback_extracts_function_names() -> None:
    refs = extract_symbol_refs_fallback(_PY_HUNK)
    names = {r.name for r in refs}
    assert "my_function" in names or "helper" in names or "old_function" in names


def test_fallback_returns_list_of_symbol_refs() -> None:
    refs = extract_symbol_refs_fallback(_PY_HUNK)
    for r in refs:
        assert isinstance(r, SymbolRef)
        assert r.name
        assert r.kind  # non-empty string (node type or "identifier")


def test_extract_symbol_refs_unknown_language_falls_back() -> None:
    """Unknown language should not raise; returns fallback results or empty list."""
    refs = extract_symbol_refs(_PY_HUNK, "UnknownLang")
    assert isinstance(refs, list)


def test_extract_symbol_refs_returns_list() -> None:
    refs = extract_symbol_refs(_PY_HUNK, "Python")
    assert isinstance(refs, list)


def test_symbol_ref_dataclass() -> None:
    ref = SymbolRef(name="foo", kind="function", line=1)
    assert ref.name == "foo"
    assert ref.kind == "function"
    assert ref.line == 1
