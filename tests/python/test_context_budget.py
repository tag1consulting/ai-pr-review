"""Tests for ai_pr_review.context.budget — E3.S3."""

from ai_pr_review.context.budget import build_context_block
from ai_pr_review.context.symbols import Definition


def _def(
    symbol: str = "my_func",
    file: str = "src/foo.py",
    line: int = 10,
    snippet: str = "def my_func(): pass",
    proximity: str = "repo",
) -> Definition:
    return Definition(symbol=symbol, file=file, line=line, snippet=snippet, proximity=proximity)


def test_empty_definitions_returns_empty() -> None:
    assert build_context_block([]) == ""


def test_block_starts_with_symbol_context_tag() -> None:
    result = build_context_block([_def()])
    assert result.startswith("<symbol-context>")


def test_block_ends_with_closing_tag() -> None:
    result = build_context_block([_def()])
    assert result.rstrip().endswith("</symbol-context>")


def test_snippet_included() -> None:
    result = build_context_block([_def(snippet="def my_func(): pass")])
    assert "my_func" in result


def test_file_and_line_included() -> None:
    result = build_context_block([_def(file="src/foo.py", line=42)])
    assert "src/foo.py" in result
    assert "42" in result


def test_token_budget_limits_output() -> None:
    # Create many large definitions
    defs = [_def(snippet="x" * 200, symbol=f"f{i}") for i in range(50)]
    result = build_context_block(defs, max_tokens=10)
    # Very small budget — should either return empty or a tiny block
    # (depends on whether even the header fits)
    assert len(result) <= 10 * 4 + 200  # rough upper bound


def test_same_file_definitions_prioritized() -> None:
    """same-file definitions should appear before repo-wide ones."""
    repo_def = _def(symbol="repo_fn", proximity="repo", snippet="def repo_fn(): pass")
    same_file_def = _def(
        symbol="local_fn", proximity="same-file", snippet="def local_fn(): pass"
    )
    result = build_context_block([repo_def, same_file_def])
    assert result.index("local_fn") < result.index("repo_fn")


def test_multiple_definitions() -> None:
    defs = [_def(symbol=f"func_{i}", snippet=f"def func_{i}(): ...") for i in range(3)]
    result = build_context_block(defs)
    for i in range(3):
        assert f"func_{i}" in result
