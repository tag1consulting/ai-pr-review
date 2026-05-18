"""Fault-injection tests for ai_pr_review/context/treesitter.py fail-soft paths."""

from __future__ import annotations

import logging
import sys
import types


def test_missing_package_is_failsoft(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    sys.modules.pop("ai_pr_review.context.treesitter", None)

    from ai_pr_review.context.treesitter import extract_symbol_refs

    with caplog.at_level(logging.WARNING, logger="ai_pr_review.context.treesitter"):
        result = extract_symbol_refs("+def foo(): pass", "python")

    assert result == []
    assert any("[ai-pr-review] WARNING:" in r.message for r in caplog.records)


def test_grammar_load_failure_is_failsoft(monkeypatch, caplog):
    sys.modules.pop("ai_pr_review.context.treesitter", None)

    mock_pack = types.ModuleType("tree_sitter_language_pack")

    def _raise(name):
        raise Exception("no grammar")

    mock_pack.get_parser = _raise
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", mock_pack)

    from ai_pr_review.context.treesitter import extract_symbol_refs

    with caplog.at_level(logging.WARNING, logger="ai_pr_review.context.treesitter"):
        result = extract_symbol_refs("+def foo(): pass", "python")

    assert result == []
    assert any("[ai-pr-review] WARNING:" in r.message for r in caplog.records)
