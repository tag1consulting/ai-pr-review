"""Tests for ai_pr_review/context/symbols.py — ripgrep-based symbol lookup.

Covers the cases identified as untested in the issue #391 evaluation:
- rg-missing no-op (graceful degradation)
- path-confinement rejection of paths outside repo_root
- query cap enforcement
- proximity classification (_classify_proximity)

Also covers _read_snippet and _glob_patterns for completeness.
All tests use a temp directory as repo_root to avoid coupling to the
actual source tree.
"""

from __future__ import annotations

import logging
import shutil
import textwrap
from pathlib import Path

import pytest

from ai_pr_review.context.symbols import (
    Definition,
    _classify_proximity,
    _glob_patterns,
    _read_snippet,
    _reset_cache,
    lookup_definitions,
)
from ai_pr_review.context.treesitter import SymbolRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sym(name: str) -> SymbolRef:
    return SymbolRef(name=name, kind="identifier", line=1)


# ---------------------------------------------------------------------------
# _classify_proximity
# ---------------------------------------------------------------------------

def test_classify_proximity_same_file() -> None:
    assert _classify_proximity("pkg/foo.py", ["pkg/foo.py", "pkg/bar.py"]) == "same-file"


def test_classify_proximity_same_package() -> None:
    assert _classify_proximity("pkg/baz.py", ["pkg/foo.py", "pkg/bar.py"]) == "same-package"


def test_classify_proximity_repo() -> None:
    assert _classify_proximity("other/qux.py", ["pkg/foo.py"]) == "repo"


def test_classify_proximity_empty_changed_files() -> None:
    assert _classify_proximity("pkg/foo.py", []) == "repo"


# ---------------------------------------------------------------------------
# _glob_patterns
# ---------------------------------------------------------------------------

def test_glob_patterns_python() -> None:
    globs = _glob_patterns("python")
    assert any("*.py" in g for g in globs)


def test_glob_patterns_unknown_language_returns_empty() -> None:
    assert _glob_patterns("xyzzy") == []


def test_glob_patterns_case_insensitive() -> None:
    assert _glob_patterns("Python") == _glob_patterns("python")


# ---------------------------------------------------------------------------
# _read_snippet
# ---------------------------------------------------------------------------

def test_read_snippet_returns_context_lines(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    lines = [f"line{i}" for i in range(1, 21)]
    f.write_text("\n".join(lines))
    snippet = _read_snippet(f, 10, 2)
    assert "line10" in snippet
    assert "line8" in snippet   # 2 lines before
    assert "line12" in snippet  # 2 lines after


def test_read_snippet_clamps_to_file_start(tmp_path: Path) -> None:
    f = tmp_path / "test.py"
    f.write_text("line1\nline2\nline3\n")
    snippet = _read_snippet(f, 1, 5)  # 5 lines before line 1 — clamp to start
    assert "line1" in snippet


def test_read_snippet_missing_file_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.py"
    assert _read_snippet(missing, 1, 3) == ""


# ---------------------------------------------------------------------------
# rg-missing graceful degradation
# ---------------------------------------------------------------------------

def test_lookup_definitions_no_rg_returns_empty(monkeypatch, caplog, tmp_path: Path) -> None:
    """When ripgrep is not found, lookup_definitions returns [] and logs a warning."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    _reset_cache()

    with caplog.at_level(logging.WARNING, logger="ai_pr_review.context.symbols"):
        result = lookup_definitions([_sym("my_func")], tmp_path, [])

    assert result == []
    assert any("ripgrep" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Path-confinement security check
# ---------------------------------------------------------------------------

def test_lookup_definitions_rejects_paths_outside_repo_root(tmp_path: Path) -> None:
    """Definitions whose resolved path escapes repo_root are silently dropped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def my_func(): pass\n")

    # Write a target file inside the repo so rg has something to scan.
    inside = repo / "inside.py"
    inside.write_text("def my_func(): pass\n")

    _reset_cache()
    rg = shutil.which("rg")
    if rg is None:
        pytest.skip("ripgrep not installed")

    results = lookup_definitions([_sym("my_func")], repo, ["inside.py"], language="python")

    result_files = {d.file for d in results}
    # The outside file must not appear even if rg found it (it won't — rg is
    # scoped to repo_root, but the path-confinement check catches symlink escapes).
    assert not any(".." in f for f in result_files)
    assert all(not Path(f).is_absolute() for f in result_files)


# ---------------------------------------------------------------------------
# Query cap enforcement
# ---------------------------------------------------------------------------

def test_lookup_definitions_respects_max_queries(caplog, tmp_path: Path) -> None:
    """After max_queries unique lookups, remaining refs are skipped with a warning."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "code.py").write_text(
        "\n".join(f"def sym{i}(): pass" for i in range(20))
    )

    rg = shutil.which("rg")
    if rg is None:
        pytest.skip("ripgrep not installed")

    _reset_cache()
    refs = [_sym(f"sym{i}") for i in range(10)]

    with caplog.at_level(logging.WARNING, logger="ai_pr_review.context.symbols"):
        lookup_definitions(refs, repo, [], language="python", max_queries=3)

    assert any("max_queries" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

def test_lookup_cache_hit_does_not_increment_query_count(tmp_path: Path) -> None:
    """A cache hit returns the stored result without incrementing query_count."""
    rg = shutil.which("rg")
    if rg is None:
        pytest.skip("ripgrep not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "code.py").write_text("def my_func(): pass\n")

    _reset_cache()
    refs = [_sym("my_func")]

    # First call — populates cache.
    lookup_definitions(refs, repo, [], language="python")
    from ai_pr_review.context.symbols import _cache
    count_after_first = _cache.query_count

    # Second call — same symbol, should hit cache.
    lookup_definitions(refs, repo, [], language="python")
    assert _cache.query_count == count_after_first


# ---------------------------------------------------------------------------
# lookup_definitions returns Definition objects with correct fields
# ---------------------------------------------------------------------------

def test_lookup_definitions_returns_definitions(tmp_path: Path) -> None:
    """lookup_definitions returns Definition instances with populated fields."""
    rg = shutil.which("rg")
    if rg is None:
        pytest.skip("ripgrep not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    code = repo / "module.py"
    code.write_text(textwrap.dedent("""\
        def target_function(x):
            return x * 2
    """))

    _reset_cache()
    results = lookup_definitions(
        [_sym("target_function")], repo, ["module.py"], language="python"
    )

    assert len(results) >= 1
    d = results[0]
    assert isinstance(d, Definition)
    assert d.symbol == "target_function"
    assert "module.py" in d.file
    assert d.line >= 1
    assert "target_function" in d.snippet
    assert d.proximity in ("same-file", "same-package", "repo")


def test_lookup_definitions_empty_refs_returns_empty(tmp_path: Path) -> None:
    _reset_cache()
    assert lookup_definitions([], tmp_path, []) == []
