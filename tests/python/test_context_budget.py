"""Tests for ai_pr_review.context.budget — E3.S3."""

from ai_pr_review.context.budget import build_context_block, estimate_tokens
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


# --- estimate_tokens CJK-awareness (#848 item 6) ---


def test_ascii_estimate_unchanged_from_original_formula() -> None:
    """No-regression-by-construction guard: for text with no CJK
    codepoints, the result must be bit-identical to the pre-#848 formula
    int(len(text) / 4 * 1.1)."""
    text = "def my_func(): return 'hello world' * 10\n" * 5
    assert estimate_tokens(text) == int(len(text) / 4 * 1.1)


def test_empty_string_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_han_text_estimates_near_one_token_per_char() -> None:
    text = "这是一段中文文本用于测试令牌估算"  # 16 Han characters
    estimate = estimate_tokens(text)
    # ~1.1 tokens/char, materially higher than the old len/4*1.1 (~4.4)
    assert estimate == int(len(text) * 1.1)
    assert estimate > int(len(text) / 4 * 1.1) * 3


def test_hiragana_text_estimates_near_one_token_per_char() -> None:
    text = "これはテストのための日本語のテキストです"
    assert estimate_tokens(text) == int(len(text) * 1.1)


def test_hangul_text_estimates_near_one_token_per_char() -> None:
    text = "이것은 토큰 추정을 위한 테스트 한국어 텍스트입니다"
    non_space_len = len(text.replace(" ", ""))
    # Spaces are ASCII, not CJK -- confirm the split correctly separates them.
    assert estimate_tokens(text) == int((non_space_len + text.count(" ") / 4) * 1.1)


def test_fullwidth_forms_are_treated_as_cjk() -> None:
    text = "ＡＢＣ"  # fullwidth Latin "ABC"
    assert estimate_tokens(text) == int(len(text) * 1.1)


def test_mixed_ascii_and_cjk_sums_both_branches() -> None:
    ascii_part = "function name: "
    cjk_part = "关键字段"
    combined = ascii_part + cjk_part
    expected = int((len(ascii_part) / 4 + len(cjk_part)) * 1.1)
    assert estimate_tokens(combined) == expected


def test_large_ascii_input_does_not_regress_performance() -> None:
    """Sanity check that the regex-based split stays linear, not quadratic,
    on a large input (this runs against entire diffs in practice)."""
    text = "x" * 2_000_000
    # Should complete near-instantly; a quadratic implementation here would
    # be conspicuously slow even at this size.
    assert estimate_tokens(text) == int(len(text) / 4 * 1.1)


def test_context_block_truncates_earlier_with_cjk_snippets() -> None:
    """A CJK-heavy definition consumes more of the budget than the old
    formula would have credited it for, so truncation kicks in sooner."""
    cjk_def = _def(symbol="cjk_fn", snippet="这是一个非常长的中文函数说明文本" * 10)
    ascii_def = _def(symbol="ascii_fn", snippet="x" * 10, proximity="repo")
    # A budget sized to fit both under the old (undercounting) formula but
    # not under the new one.
    result = build_context_block([cjk_def, ascii_def], max_tokens=60)
    assert "cjk_fn" not in result or "ascii_fn" not in result
