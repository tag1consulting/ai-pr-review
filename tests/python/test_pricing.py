"""Tests for ai_pr_review.pricing."""

from ai_pr_review.pricing import (
    TokenEntry,
    emit_token_table,
    format_cost,
    model_pricing,
    parse_token_log_entry,
)

_SAMPLE_PRICING = [
    {
        "patterns": ["claude-sonnet-4-6"],
        "display_name": "Sonnet 4.6",
        "input_rate": 3000000,
        "output_rate": 15000000,
        "cache_write_rate": 3750000,
        "cache_read_rate": 300000,
    }
]


def test_format_cost_zero() -> None:
    assert format_cost(0) == "$0.0000"


def test_format_cost_nonzero() -> None:
    # 10000 units = $1.0000
    assert format_cost(10000) == "$1.0000"


def test_format_cost_partial() -> None:
    assert format_cost(1) == "$0.0001"


def test_model_pricing_match() -> None:
    rates = model_pricing("claude-sonnet-4-6", _SAMPLE_PRICING)
    assert rates.display_name == "Sonnet 4.6"
    assert rates.input_rate == 3000000


def test_model_pricing_unknown_returns_zeros() -> None:
    rates = model_pricing("unknown-model-xyz", _SAMPLE_PRICING)
    assert rates.input_rate == 0
    assert rates.output_rate == 0


def test_emit_token_table_no_cache() -> None:
    log = [
        TokenEntry(agent="code-reviewer", model="claude-sonnet-4-6", input_tokens=1000, output_tokens=500),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING)
    assert "| Agent |" in table
    assert "code-reviewer" in table
    assert "Sonnet 4.6" in table
    # 6-column: no Cache Write / Cache Read columns
    assert "Cache Write" not in table


def test_emit_token_table_with_cache() -> None:
    log = [
        TokenEntry(
            agent="security-reviewer",
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=200,
            cache_read_tokens=50,
        ),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING)
    assert "Cache Write" in table
    assert "Cache Read" in table


def test_emit_token_table_total_row() -> None:
    log = [
        TokenEntry(agent="a1", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
        TokenEntry(agent="a2", model="claude-sonnet-4-6", input_tokens=200, output_tokens=100),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING)
    assert "**Total**" in table


def test_parse_token_log_entry() -> None:
    entry = "code-reviewer: model=claude-sonnet-4-6 input=1000 output=500 cache_creation=100 cache_read=50"
    te = parse_token_log_entry(entry)
    assert te is not None
    assert te.agent == "code-reviewer"
    assert te.model == "claude-sonnet-4-6"
    assert te.input_tokens == 1000
    assert te.output_tokens == 500
    assert te.cache_creation_tokens == 100
    assert te.cache_read_tokens == 50
