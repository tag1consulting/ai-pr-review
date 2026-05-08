#!/usr/bin/env bash
# lib/pricing.sh — token pricing and cost estimation for review.sh.
#
# Sourced by review.sh (via main() after SCRIPT_DIR is set). Exports:
#   model_pricing       — per-model rate tuple lookup
#   model_display_name  — pretty name for markdown tables
#   format_cost         — USD formatting helper
#   emit_token_table    — full agent-cost markdown table
#   emit_token_table_rows  — alias kept for callers written before the header
#                            was merged into emit_token_table
#
# Contract: caller must set MODEL_PRICING_FILE to the path of config/model-pricing.json
# before calling model_pricing / model_display_name. emit_token_table reads the
# TOKEN_LOG array populated by call_agent / call_agent_bg in lib/agents.sh.

# Returns four space-separated rates:
#   input_rate output_rate cache_write_rate cache_read_rate
# Cache rates default to 0 when the pricing entry predates the cache-column
# extension; callers should guard with `[[ $rate -gt 0 ]]` before costing.
model_pricing() {
  local model="$1"
  local m result
  if [[ ! -f "${MODEL_PRICING_FILE:-}" ]]; then
    echo "WARNING: model_pricing: MODEL_PRICING_FILE not found ('${MODEL_PRICING_FILE:-unset}'); cost estimates will show as \$0." >&2
    echo "0 0 0 0"
    return
  fi
  m=$(echo "$model" | tr '[:upper:]' '[:lower:]')
  if ! result=$(jq -r --arg m "$m" '
    first(
      .[] | select(.patterns[] as $p | ($m | test($p)))
      | "\(.input_rate) \(.output_rate) \(.cache_write_rate // 0) \(.cache_read_rate // 0)"
    ) // "0 0 0 0"
  ' "$MODEL_PRICING_FILE" 2>/dev/null); then
    echo "WARNING: model_pricing: jq failed reading '${MODEL_PRICING_FILE}' for model '${model}'; cost will be unavailable." >&2
    result="0 0 0 0"
  fi
  echo "${result:-0 0 0 0}"
}


# Human-readable model display name: "Sonnet 4.6", "GPT-4o mini", etc.
model_display_name() {
  local model="$1"
  local m result
  if [[ ! -f "${MODEL_PRICING_FILE:-}" ]]; then
    echo "$model"
    return
  fi
  m=$(echo "$model" | tr '[:upper:]' '[:lower:]')
  result=$(jq -r --arg m "$m" '
    first(
      .[] | select(.patterns[] as $p | ($m | test($p)))
      | .display_name
    ) // ""
  ' "$MODEL_PRICING_FILE" 2>/dev/null) || result=""
  echo "${result:-$model}"
}


# Format microdollars (millionths of a dollar) as $X.XXXXXX
format_cost() {
  local microdollars="$1"
  # microdollars = tokens * rate / 1_000_000, where rate is in nanodollars/token * 1e6
  # Actually our unit: rate is in units of $0.000001 per token × 1e6 = dollars/token × 1e12
  # Simpler: pass raw integer from calculation and format as dollars with 4 decimal places
  # We work in units of $0.0001 (tenths of a cent) to keep integers manageable
  local whole=$(( microdollars / 10000 ))
  local frac=$(( microdollars % 10000 ))
  printf '$%d.%04d' "$whole" "$frac"
}


# ---------------------------------------------------------------------------
# Emit token usage table rows to stdout.
# Shared by the PR comment <details> block and the step summary.
# Uses awk for cost arithmetic to avoid bash integer overflow on large
# token counts multiplied by rate constants (up to 75,000,000).
# ---------------------------------------------------------------------------
# Emit the full markdown token table (header + rows + total) to stdout.
# Adaptive: if any row had cache activity (cache_creation or cache_read > 0),
# emits two extra columns "Cache Write" and "Cache Read"; otherwise uses the
# original 6-column layout so non-caching runs stay visually identical.
#
# Rationale for the two-pass approach: we don't know whether to emit the
# cache columns until we've scanned every entry. Rather than snapshot state
# in a global, we compute totals in pass 1 and emit rows in pass 2.
emit_token_table() {
  # Pass 1: decide column layout
  # Grep patterns anchor on line-start OR whitespace so an agent name or
  # model_id containing "cache_creation=" as a substring could not shadow
  # the real field. Defensive — model IDs come from a fixed list today.
  local any_cache=0
  local entry
  for entry in "${TOKEN_LOG[@]}"; do
    local cw_tok cr_tok
    cw_tok=$(echo "$entry" | grep -oE '(^| )cache_creation=[0-9]+' | sed 's/.*cache_creation=//' || echo "")
    cr_tok=$(echo "$entry" | grep -oE '(^| )cache_read=[0-9]+' | sed 's/.*cache_read=//' || echo "")
    if [[ "${cw_tok:-0}" -gt 0 || "${cr_tok:-0}" -gt 0 ]]; then any_cache=1; break; fi
  done

  # Header
  if [[ "$any_cache" -eq 1 ]]; then
    echo "| Agent | Model | Input | Output | Cache Write | Cache Read | Total | Est. Cost |"
    echo "|-------|-------|------:|-------:|------------:|-----------:|------:|----------:|"
  else
    echo "| Agent | Model | Input | Output | Total | Est. Cost |"
    echo "|-------|-------|------:|-------:|------:|----------:|"
  fi

  # Pass 2: rows + totals
  local total_in=0 total_out=0 total_cw=0 total_cr=0 total_cost=0 any_unknown=0
  for entry in "${TOKEN_LOG[@]}"; do
    local agent_name in_tok out_tok cw_tok cr_tok model_id row_total
    local in_rate out_rate cw_rate cr_rate cost_display model_short
    agent_name="${entry%%:*}"
    in_tok=$(echo "$entry" | grep -oE '(^| )input=[0-9]+' | sed 's/.*input=//' || echo "")
    out_tok=$(echo "$entry" | grep -oE '(^| )output=[0-9]+' | sed 's/.*output=//' || echo "")
    cw_tok=$(echo "$entry" | grep -oE '(^| )cache_creation=[0-9]+' | sed 's/.*cache_creation=//' || echo "")
    cr_tok=$(echo "$entry" | grep -oE '(^| )cache_read=[0-9]+' | sed 's/.*cache_read=//' || echo "")
    # Warn on malformed TOKEN_LOG entries. input/output are always required — a
    # missing/non-numeric value silently zeroes the row and understates the
    # total. cache_creation/cache_read legitimately absent on non-caching
    # providers, so default to 0 without warning.
    if ! [[ "$in_tok" =~ ^[0-9]+$ ]]; then
      echo "WARNING: emit_token_table: malformed TOKEN_LOG entry for '${agent_name}' — missing or non-numeric input count. Entry: '${entry}'" >&2
      in_tok=0
    fi
    if ! [[ "$out_tok" =~ ^[0-9]+$ ]]; then
      echo "WARNING: emit_token_table: malformed TOKEN_LOG entry for '${agent_name}' — missing or non-numeric output count. Entry: '${entry}'" >&2
      out_tok=0
    fi
    cw_tok="${cw_tok:-0}"
    cr_tok="${cr_tok:-0}"
    model_id=$(echo "$entry" | grep -oE '(^| )model=[^ ]+' | sed 's/.*model=//' || echo "unknown")
    row_total=$(( in_tok + out_tok + cw_tok + cr_tok ))
    read -r in_rate out_rate cw_rate cr_rate <<< "$(model_pricing "$model_id")"
    if [[ "$in_rate" -eq 0 && "$out_rate" -eq 0 ]]; then
      cost_display="n/a"
      any_unknown=1
    else
      # awk-based arithmetic avoids 64-bit overflow on large token counts.
      # Cost: input*rate + output*rate + cache_creation*write_rate + cache_read*read_rate
      # Rates are in dollars/1M-tokens × 1e6; divide by 10^8 to get $0.0001 units.
      local cost_units
      cost_units=$(awk \
        -v it="${in_tok:-0}"  -v ir="${in_rate:-0}" \
        -v ot="${out_tok:-0}" -v or_="${out_rate:-0}" \
        -v wt="${cw_tok:-0}"  -v wr="${cw_rate:-0}" \
        -v rt="${cr_tok:-0}"  -v rr="${cr_rate:-0}" \
        'BEGIN {printf "%d", (it*ir + ot*or_ + wt*wr + rt*rr) / 100000000}')
      cost_display=$(format_cost "$cost_units")
      total_cost=$(( total_cost + cost_units ))
    fi
    model_short=$(model_display_name "$model_id")
    if [[ "$any_cache" -eq 1 ]]; then
      echo "| ${agent_name} | ${model_short} | ${in_tok} | ${out_tok} | ${cw_tok} | ${cr_tok} | ${row_total} | ${cost_display} |"
    else
      echo "| ${agent_name} | ${model_short} | ${in_tok} | ${out_tok} | ${row_total} | ${cost_display} |"
    fi
    total_in=$(( total_in + in_tok ))
    total_out=$(( total_out + out_tok ))
    total_cw=$(( total_cw + cw_tok ))
    total_cr=$(( total_cr + cr_tok ))
  done

  # Total row
  local total_cost_display
  if [[ "$any_unknown" -eq 1 ]]; then
    total_cost_display="$(format_cost "$total_cost")+"
  else
    total_cost_display="$(format_cost "$total_cost")"
  fi
  local grand_total=$(( total_in + total_out + total_cw + total_cr ))
  if [[ "$any_cache" -eq 1 ]]; then
    echo "| **Total** | | **${total_in}** | **${total_out}** | **${total_cw}** | **${total_cr}** | **${grand_total}** | **${total_cost_display}** |"
  else
    echo "| **Total** | | **${total_in}** | **${total_out}** | **${grand_total}** | **${total_cost_display}** |"
  fi
}


# Backward-compat shim: the old emit_token_table_rows only emitted rows
# (header was echo'd separately). Keep it for anything that still calls it
# directly; it now emits the whole table (header + rows + total) via the new
# entry point. Call sites below have been updated to not emit their own header.
emit_token_table_rows() {
  emit_token_table
}
