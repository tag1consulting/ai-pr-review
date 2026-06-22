"""Model pricing and token-table rendering.

Ports lib/pricing.sh: model_pricing(), model_display_name(),
format_cost(), emit_token_table().
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ModelRates:
    display_name: str
    input_rate: int
    output_rate: int
    cache_write_rate: int = 0
    cache_read_rate: int = 0


@dataclass
class TokenEntry:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    max_output_tokens: int = 0  # 0 means no cap shown


def load_pricing(pricing_file: str) -> list[dict[str, object]]:
    path = Path(pricing_file)
    if not path.is_file():
        print(
            f"WARNING: model_pricing: pricing file not found '{pricing_file}'; "
            "cost estimates will show as $0.",
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: model_pricing: could not load pricing file: {exc}", file=sys.stderr)
        return []


def _as_int(v: object, default: int = 0) -> int:
    try:
        return int(v)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return default


def model_pricing(model_id: str, pricing_data: list[dict[str, object]]) -> ModelRates:
    """Return ModelRates for model_id, defaulting to zero rates if unknown."""
    for entry in pricing_data:
        patterns = entry.get("patterns", [])
        if not isinstance(patterns, list):
            continue
        for pat in patterns:
            if re.search(str(pat), model_id):
                return ModelRates(
                    display_name=str(entry.get("display_name", model_id)),
                    input_rate=_as_int(entry.get("input_rate", 0)),
                    output_rate=_as_int(entry.get("output_rate", 0)),
                    cache_write_rate=_as_int(entry.get("cache_write_rate", 0)),
                    cache_read_rate=_as_int(entry.get("cache_read_rate", 0)),
                )
    return ModelRates(display_name=model_id, input_rate=0, output_rate=0)


def format_cost(microdollars: int) -> str:
    """Format an integer in $0.0001 units as a dollar string."""
    whole = microdollars // 10000
    frac = microdollars % 10000
    return f"${whole}.{frac:04d}"


def _row_cost(entry: TokenEntry, rates: ModelRates) -> int | None:
    """Return cost in $0.0001 units, or None if rates are unknown."""
    if rates.input_rate == 0 and rates.output_rate == 0:
        return None
    cost = (
        entry.input_tokens * rates.input_rate
        + entry.output_tokens * rates.output_rate
        + entry.cache_creation_tokens * rates.cache_write_rate
        + entry.cache_read_tokens * rates.cache_read_rate
    ) // 100_000_000
    return cost


def emit_token_table(
    token_log: list[TokenEntry],
    pricing_data: list[dict[str, object]],
    *,
    context_tokens: int = 0,
    profile_tokens: int = 0,
    sarif_elapsed_s: float | None = None,
) -> str:
    """Render the token-usage markdown table. Matches bash emit_token_table output."""
    any_cache = any(
        e.cache_creation_tokens > 0 or e.cache_read_tokens > 0
        for e in token_log
    )

    lines: list[str] = []

    if any_cache:
        lines.append(
            "| Agent | Model | Input | Output | Cache Write | Cache Read | Total | Est. Cost |"
        )
        lines.append(
            "|-------|-------|------:|-------:|------------:|-----------:|------:|----------:|"
        )
    else:
        lines.append("| Agent | Model | Input | Output | Total | Est. Cost |")
        lines.append("|-------|-------|------:|-------:|------:|----------:|")

    total_in = total_out = total_cw = total_cr = total_cost = 0
    any_unknown = False

    for entry in token_log:
        rates = model_pricing(entry.model, pricing_data)
        cost_units = _row_cost(entry, rates)
        if cost_units is None:
            cost_display = "n/a"
            any_unknown = True
        else:
            cost_display = format_cost(cost_units)
            total_cost += cost_units

        row_total = (
            entry.input_tokens
            + entry.output_tokens
            + entry.cache_creation_tokens
            + entry.cache_read_tokens
        )

        output_cell = (
            f"{entry.output_tokens} / {entry.max_output_tokens}"
            if entry.max_output_tokens > 0
            else f"{entry.output_tokens}"
        )

        if any_cache:
            lines.append(
                f"| {entry.agent} | {rates.display_name} | {entry.input_tokens} | "
                f"{output_cell} | {entry.cache_creation_tokens} | "
                f"{entry.cache_read_tokens} | {row_total} | {cost_display} |"
            )
        else:
            lines.append(
                f"| {entry.agent} | {rates.display_name} | {entry.input_tokens} | "
                f"{output_cell} | {row_total} | {cost_display} |"
            )

        total_in += entry.input_tokens
        total_out += entry.output_tokens
        total_cw += entry.cache_creation_tokens
        total_cr += entry.cache_read_tokens

    grand_total = total_in + total_out + total_cw + total_cr
    total_cost_str = format_cost(total_cost) + ("+" if any_unknown else "")

    if any_cache:
        lines.append(
            f"| **Total** | | **{total_in}** | **{total_out}** | "
            f"**{total_cw}** | **{total_cr}** | **{grand_total}** | **{total_cost_str}** |"
        )
    else:
        lines.append(
            f"| **Total** | | **{total_in}** | **{total_out}** | "
            f"**{grand_total}** | **{total_cost_str}** |"
        )

    if context_tokens > 0:
        if any_cache:
            # 8-col: Agent | Model | Input(value) | Output | CW | CR | Total | Cost
            lines.append(
                f"| Context enrichment | *(context)* | {context_tokens}"
                " | — | — | — | — | — |"
            )
        else:
            # 6-col: Agent | Model | Input(value) | Output | Total | Cost
            lines.append(
                f"| Context enrichment | *(context)* | {context_tokens}"
                " | — | — | — |"
            )

    if profile_tokens > 0:
        if any_cache:
            # 8-col: Agent | Model | Input(value) | Output | CW | CR | Total | Cost
            lines.append(
                f"| Language profiles | *(profile)* | {profile_tokens}"
                " | — | — | — | — | — |"
            )
        else:
            # 6-col: Agent | Model | Input(value) | Output | Total | Cost
            lines.append(
                f"| Language profiles | *(profile)* | {profile_tokens}"
                " | — | — | — |"
            )

    if sarif_elapsed_s is not None and math.isfinite(sarif_elapsed_s) and sarif_elapsed_s >= 0:
        if any_cache:
            # 8-col: Agent | Model | Input | Output(value) | CW | CR | Total | Cost
            lines.append(
                f"| SARIF ingestion | *(timing)* | — | {sarif_elapsed_s:.2f}s"
                " | — | — | — | — |"
            )
        else:
            # 6-col: Agent | Model | Input | Output(value) | Total | Cost
            lines.append(
                f"| SARIF ingestion | *(timing)* | — | {sarif_elapsed_s:.2f}s"
                " | — | — |"
            )

    return "\n".join(lines)


def parse_token_log_entry(entry: str) -> TokenEntry:
    """Parse a bash TOKEN_LOG entry string into a TokenEntry."""

    def _extract(pattern: str, text: str, default: int = 0) -> int:
        m = re.search(pattern, text)
        return int(m.group(1)) if m else default

    agent = entry.split(":")[0] if ":" in entry else "unknown"
    model_m = re.search(r"(?:^| )model=(\S+)", entry)
    model = model_m.group(1) if model_m else "unknown"

    return TokenEntry(
        agent=agent,
        model=model,
        input_tokens=_extract(r"(?:^| )input=(\d+)", entry),
        output_tokens=_extract(r"(?:^| )output=(\d+)", entry),
        cache_creation_tokens=_extract(r"(?:^| )cache_creation=(\d+)", entry),
        cache_read_tokens=_extract(r"(?:^| )cache_read=(\d+)", entry),
    )
