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


def test_emit_token_table_max_output_tokens_shown() -> None:
    log = [
        TokenEntry(
            agent="code-reviewer",
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=1234,
            max_output_tokens=16384,
        ),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING)
    assert "1234 / 16384" in table
    # Total row must use raw integer, not the formatted "N / cap" string
    total_line = next(line for line in table.splitlines() if "**Total**" in line)
    assert "**1234**" in total_line
    assert "16384" not in total_line


def test_emit_token_table_max_output_tokens_zero_omitted() -> None:
    log = [
        TokenEntry(
            agent="code-reviewer",
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
            max_output_tokens=0,
        ),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING)
    assert " / " not in table


def test_emit_token_table_context_enrichment_row() -> None:
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, context_tokens=2048)
    assert "Context enrichment" in table
    assert "2048" in table
    assert "*(context)*" in table


def test_emit_token_table_context_enrichment_zero_omitted() -> None:
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, context_tokens=0)
    assert "Context enrichment" not in table


def test_emit_token_table_sarif_row() -> None:
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, sarif_elapsed_s=0.34)
    assert "SARIF ingestion" in table
    assert "0.34s" in table
    assert "*(timing)*" in table


def test_emit_token_table_sarif_none_omitted() -> None:
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, sarif_elapsed_s=None)
    assert "SARIF ingestion" not in table


def test_emit_token_table_baseline_6col_unchanged() -> None:
    """Default params produce byte-for-byte identical 6-col output (story 4-3 guardrail)."""
    log = [
        TokenEntry(agent="code-reviewer", model="claude-sonnet-4-6", input_tokens=1000, output_tokens=500),
    ]
    expected = (
        "| Agent | Model | Input | Output | Total | Est. Cost |\n"
        "|-------|-------|------:|-------:|------:|----------:|\n"
        "| code-reviewer | Sonnet 4.6 | 1000 | 500 | 1500 | $0.0105 |\n"
        "| **Total** | | **1000** | **500** | **1500** | **$0.0105** |"
    )
    assert emit_token_table(log, _SAMPLE_PRICING) == expected


def test_emit_token_table_baseline_8col_unchanged() -> None:
    """Default params produce byte-for-byte identical 8-col output (story 4-3 guardrail)."""
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
    expected = (
        "| Agent | Model | Input | Output | Cache Write | Cache Read | Total | Est. Cost |\n"
        "|-------|-------|------:|-------:|------------:|-----------:|------:|----------:|\n"
        "| security-reviewer | Sonnet 4.6 | 1000 | 500 | 200 | 50 | 1750 | $0.0112 |\n"
        "| **Total** | | **1000** | **500** | **200** | **50** | **1750** | **$0.0112** |"
    )
    assert emit_token_table(log, _SAMPLE_PRICING) == expected


def test_emit_token_table_supplementary_rows_8col_column_count() -> None:
    log = [
        TokenEntry(
            agent="a",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=10,
            cache_read_tokens=5,
        ),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, context_tokens=512, sarif_elapsed_s=0.5)
    lines = table.splitlines()
    header_pipes = lines[0].count("|")
    for line in lines[4:]:  # supplementary rows after Total
        assert line.count("|") == header_pipes, f"Column mismatch in supplementary row: {line!r}"


def test_emit_token_table_max_output_tokens_8col_total_row() -> None:
    log = [
        TokenEntry(
            agent="a",
            model="claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=1234,
            cache_creation_tokens=200,
            cache_read_tokens=50,
            max_output_tokens=16384,
        ),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING)
    assert "1234 / 16384" in table
    total_line = [ln for ln in table.splitlines() if "**Total**" in ln][0]
    assert "1234 / 16384" not in total_line
    assert "**1234**" in total_line


def test_emit_token_table_sarif_zero_elapsed_shown() -> None:
    """sarif_elapsed_s=0.0 is not None — row must appear."""
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, sarif_elapsed_s=0.0)
    assert "SARIF ingestion" in table
    assert "0.00s" in table


def test_emit_token_table_sarif_nan_omitted() -> None:
    """sarif_elapsed_s=float('nan') must not produce a row (non-finite guard)."""
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, sarif_elapsed_s=float("nan"))
    assert "SARIF ingestion" not in table
    assert "nan" not in table


def test_emit_token_table_sarif_inf_omitted() -> None:
    """sarif_elapsed_s=float('inf') must not produce a row (non-finite guard)."""
    log = [
        TokenEntry(agent="a", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
    ]
    table = emit_token_table(log, _SAMPLE_PRICING, sarif_elapsed_s=float("inf"))
    assert "SARIF ingestion" not in table
    assert "inf" not in table


def test_emit_token_table_supplementary_rows_not_in_total_cost() -> None:
    log = [
        TokenEntry(agent="a1", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50),
        TokenEntry(agent="a2", model="claude-sonnet-4-6", input_tokens=200, output_tokens=100),
    ]
    base_table = emit_token_table(log, _SAMPLE_PRICING)
    augmented_table = emit_token_table(
        log, _SAMPLE_PRICING, context_tokens=500, sarif_elapsed_s=1.5
    )
    # Extract Total row from each; cost should be identical
    base_total_line = next(ln for ln in base_table.splitlines() if "**Total**" in ln)
    aug_total_line = next(ln for ln in augmented_table.splitlines() if "**Total**" in ln)
    assert base_total_line == aug_total_line
    # Raw token totals unchanged
    assert "**150**" in aug_total_line  # total_out = 150
    assert "**300**" in aug_total_line  # total_in = 300


# ---------------------------------------------------------------------------
# E4.S3: effective_max_tokens in _build_token_table_accordion
# ---------------------------------------------------------------------------

def test_build_token_table_accordion_effective_max_tokens(tmp_path: object) -> None:
    """effective_max_tokens overrides the roster default in the output cap column."""
    from unittest.mock import patch as _patch
    from pathlib import Path
    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage

    ar = AgentResult(
        name="code-reviewer",
        output="findings output",
        token_log=TokenUsage(
            input=100, output=80, cache_creation=0, cache_read=0, model="claude-sonnet-4-6"
        ),
        truncated=False,
        elapsed_ms=1000,
        context_tokens_used=0,
    )

    # Patch load_pricing at its source module; script_dir is arbitrary (pricing file not read)
    import ai_pr_review.cli as _cli
    with _patch("ai_pr_review.pricing.load_pricing", return_value=_SAMPLE_PRICING):
        result = _cli._build_token_table_accordion(
            [ar],
            None,
            Path("."),
            effective_max_tokens=4096,
        )
    # The output cell should render "80 / 4096" not "80 / 16384" (roster default)
    assert "80 / 4096" in result
    assert "80 / 16384" not in result


def test_build_token_table_accordion_falls_back_to_roster_default() -> None:
    """When effective_max_tokens=0, the roster default is used."""
    from unittest.mock import patch as _patch
    from pathlib import Path
    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage
    from ai_pr_review.agents.roster import AGENTS

    roster_cap = next(s.max_output_tokens for s in AGENTS if s.name == "code-reviewer")

    ar = AgentResult(
        name="code-reviewer",
        output="findings output",
        token_log=TokenUsage(
            input=100, output=80, cache_creation=0, cache_read=0, model="claude-sonnet-4-6"
        ),
        truncated=False,
        elapsed_ms=1000,
        context_tokens_used=0,
    )

    import ai_pr_review.cli as _cli
    with _patch("ai_pr_review.pricing.load_pricing", return_value=_SAMPLE_PRICING):
        result = _cli._build_token_table_accordion([ar], None, Path("."), effective_max_tokens=0)
    assert f"80 / {roster_cap}" in result
