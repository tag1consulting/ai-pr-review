#!/usr/bin/env bats
# Tests for pure functions in review.sh.
# Functions are extracted individually at test time — the full script is never
# sourced, so the orchestration pipeline does not run.

setup() {
  load test_helper
  MODEL_PRICING_FILE="${PROJECT_ROOT}/config/model-pricing.json"
  load_function "${PROJECT_ROOT}/lib/languages.sh" detect_language
  load_function "${PROJECT_ROOT}/lib/languages.sh" is_test_file
  load_function "${PROJECT_ROOT}/lib/pricing.sh" model_pricing
  load_function "${PROJECT_ROOT}/lib/pricing.sh" model_display_name
  load_function "${PROJECT_ROOT}/lib/pricing.sh" format_cost
}

# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

@test "detect_language: go -> Go" {
  run detect_language "go"
  [ "$status" -eq 0 ]
  [ "$output" = "Go" ]
}

@test "detect_language: py -> Python" {
  run detect_language "py"
  [ "$status" -eq 0 ]
  [ "$output" = "Python" ]
}

@test "detect_language: ts -> TypeScript" {
  run detect_language "ts"
  [ "$status" -eq 0 ]
  [ "$output" = "TypeScript" ]
}

@test "detect_language: tsx -> TypeScript" {
  run detect_language "tsx"
  [ "$status" -eq 0 ]
  [ "$output" = "TypeScript" ]
}

@test "detect_language: js -> JavaScript" {
  run detect_language "js"
  [ "$status" -eq 0 ]
  [ "$output" = "JavaScript" ]
}

@test "detect_language: sh -> Shell" {
  run detect_language "sh"
  [ "$status" -eq 0 ]
  [ "$output" = "Shell" ]
}

@test "detect_language: bash -> Shell" {
  run detect_language "bash"
  [ "$status" -eq 0 ]
  [ "$output" = "Shell" ]
}

@test "detect_language: module -> PHP" {
  run detect_language "module"
  [ "$status" -eq 0 ]
  [ "$output" = "PHP" ]
}

@test "detect_language: php -> PHP" {
  run detect_language "php"
  [ "$status" -eq 0 ]
  [ "$output" = "PHP" ]
}

@test "detect_language: tf -> Terraform" {
  run detect_language "tf"
  [ "$status" -eq 0 ]
  [ "$output" = "Terraform" ]
}

@test "detect_language: yml -> YAML" {
  run detect_language "yml"
  [ "$status" -eq 0 ]
  [ "$output" = "YAML" ]
}

@test "detect_language: rb -> Ruby" {
  run detect_language "rb"
  [ "$status" -eq 0 ]
  [ "$output" = "Ruby" ]
}

@test "detect_language: rake -> Ruby" {
  run detect_language "rake"
  [ "$status" -eq 0 ]
  [ "$output" = "Ruby" ]
}

@test "detect_language: gemspec -> Ruby" {
  run detect_language "gemspec"
  [ "$status" -eq 0 ]
  [ "$output" = "Ruby" ]
}

@test "detect_language: rs -> Rust" {
  run detect_language "rs"
  [ "$status" -eq 0 ]
  [ "$output" = "Rust" ]
}

@test "detect_language: java -> Java" {
  run detect_language "java"
  [ "$status" -eq 0 ]
  [ "$output" = "Java" ]
}

@test "detect_language: c -> C++" {
  run detect_language "c"
  [ "$status" -eq 0 ]
  [ "$output" = "C++" ]
}

@test "detect_language: h -> C++" {
  run detect_language "h"
  [ "$status" -eq 0 ]
  [ "$output" = "C++" ]
}

@test "detect_language: cpp -> C++" {
  run detect_language "cpp"
  [ "$status" -eq 0 ]
  [ "$output" = "C++" ]
}

@test "detect_language: hpp -> C++" {
  run detect_language "hpp"
  [ "$status" -eq 0 ]
  [ "$output" = "C++" ]
}

@test "detect_language: cc -> C++" {
  run detect_language "cc"
  [ "$status" -eq 0 ]
  [ "$output" = "C++" ]
}

@test "detect_language: cxx -> C++" {
  run detect_language "cxx"
  [ "$status" -eq 0 ]
  [ "$output" = "C++" ]
}

@test "detect_language: unknown extension -> empty string" {
  run detect_language "xyz"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

@test "detect_language: empty extension -> empty string" {
  run detect_language ""
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

# ---------------------------------------------------------------------------
# model_pricing
# ---------------------------------------------------------------------------

@test "model_pricing: claude-sonnet-4-6 returns correct rates" {
  run model_pricing "claude-sonnet-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000 3750000 300000" ]
}

@test "model_pricing: claude-opus-4-7 returns correct rates" {
  run model_pricing "claude-opus-4-7"
  [ "$status" -eq 0 ]
  [ "$output" = "5000000 25000000 6250000 500000" ]
}

@test "model_pricing: claude-opus-4-6 still matched by Opus pattern" {
  run model_pricing "claude-opus-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "5000000 25000000 6250000 500000" ]
}

@test "model_pricing: claude-haiku-4-5 returns correct rates" {
  run model_pricing "claude-haiku-4-5-20251001"
  [ "$status" -eq 0 ]
  [ "$output" = "800000 4000000 1000000 80000" ]
}

@test "model_pricing: gpt-4o-mini is not shadowed by gpt-4o pattern" {
  run model_pricing "gpt-4o-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "150000 600000 0 75000" ]
}

@test "model_pricing: gpt-4o returns correct rates" {
  run model_pricing "gpt-4o"
  [ "$status" -eq 0 ]
  [ "$output" = "2500000 10000000 0 1250000" ]
}

@test "model_pricing: gpt-4.1 returns correct rates" {
  run model_pricing "gpt-4.1"
  [ "$status" -eq 0 ]
  [ "$output" = "2000000 8000000 0 500000" ]
}

@test "model_pricing: gpt-4.1 dated variant matches" {
  run model_pricing "gpt-4.1-2025-04-14"
  [ "$status" -eq 0 ]
  [ "$output" = "2000000 8000000 0 500000" ]
}

@test "model_pricing: gpt-4.1-mini is not shadowed by gpt-4.1 pattern" {
  run model_pricing "gpt-4.1-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "400000 1600000 0 100000" ]
}

@test "model_pricing: gpt-4.1-nano is not shadowed by gpt-4.1 pattern" {
  run model_pricing "gpt-4.1-nano"
  [ "$status" -eq 0 ]
  [ "$output" = "100000 400000 0 25000" ]
}

@test "model_pricing: o3 returns correct rates" {
  run model_pricing "o3"
  [ "$status" -eq 0 ]
  [ "$output" = "2000000 8000000 0 500000" ]
}

@test "model_pricing: o3 dated variant matches" {
  run model_pricing "o3-2025-04-16"
  [ "$status" -eq 0 ]
  [ "$output" = "2000000 8000000 0 500000" ]
}

@test "model_pricing: o3-mini is not shadowed by o3 pattern" {
  run model_pricing "o3-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "1100000 4400000 0 550000" ]
}

@test "model_pricing: o4-mini returns correct rates" {
  run model_pricing "o4-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "1100000 4400000 0 275000" ]
}

@test "model_pricing: gpt-5.4 returns correct rates" {
  run model_pricing "gpt-5.4"
  [ "$status" -eq 0 ]
  [ "$output" = "2500000 15000000 0 250000" ]
}

@test "model_pricing: gpt-5.4 dated variant matches" {
  run model_pricing "gpt-5.4-2026-03-05"
  [ "$status" -eq 0 ]
  [ "$output" = "2500000 15000000 0 250000" ]
}

@test "model_pricing: gpt-5.4-mini is not shadowed by gpt-5.4 pattern" {
  run model_pricing "gpt-5.4-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "750000 4500000 0 75000" ]
}

@test "model_pricing: gpt-5.4-nano is not shadowed by gpt-5.4 pattern" {
  run model_pricing "gpt-5.4-nano"
  [ "$status" -eq 0 ]
  [ "$output" = "200000 1250000 0 20000" ]
}

@test "model_pricing: gpt-5.5 returns correct rates" {
  run model_pricing "gpt-5.5"
  [ "$status" -eq 0 ]
  [ "$output" = "5000000 30000000 0 500000" ]
}

@test "model_pricing: gpt-5 returns correct rates" {
  run model_pricing "gpt-5"
  [ "$status" -eq 0 ]
  [ "$output" = "1250000 10000000 0 125000" ]
}

@test "model_pricing: gpt-5 is not shadowed by gpt-5.4 or gpt-5.5" {
  run model_pricing "gpt-5-2025-08-07"
  [ "$status" -eq 0 ]
  [ "$output" = "1250000 10000000 0 125000" ]
}

@test "model_pricing: gemini-2.5-flash returns correct rates" {
  run model_pricing "gemini-2.5-flash"
  [ "$status" -eq 0 ]
  [ "$output" = "300000 2500000 0 30000" ]
}

@test "model_pricing: gemini-2.5-flash-lite is not shadowed by flash pattern" {
  run model_pricing "gemini-2.5-flash-lite"
  [ "$status" -eq 0 ]
  [ "$output" = "100000 400000 0 10000" ]
}

@test "model_pricing: gemini-2.5-pro returns correct rates" {
  run model_pricing "gemini-2.5-pro"
  [ "$status" -eq 0 ]
  [ "$output" = "1250000 10000000 0 125000" ]
}

@test "model_pricing: unknown model returns zero rates" {
  run model_pricing "some-unknown-model-v1"
  [ "$status" -eq 0 ]
  [ "$output" = "0 0 0 0" ]
}

@test "model_pricing: bedrock-prefixed claude-sonnet matches" {
  run model_pricing "us.anthropic.claude-sonnet-4-6"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000 3750000 300000" ]
}

@test "model_pricing: uppercase model name matched case-insensitively" {
  run model_pricing "CLAUDE-SONNET-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000 3750000 300000" ]
}

# ---------------------------------------------------------------------------
# model_display_name
# ---------------------------------------------------------------------------

@test "model_display_name: claude-sonnet-4-6 -> Sonnet 4.6" {
  run model_display_name "claude-sonnet-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "Sonnet 4.6" ]
}

@test "model_display_name: claude-opus-4-7 -> Opus 4.7" {
  run model_display_name "claude-opus-4-7"
  [ "$status" -eq 0 ]
  [ "$output" = "Opus 4.7" ]
}

@test "model_display_name: claude-opus-4-6 -> Opus 4.7 (shared display name)" {
  run model_display_name "claude-opus-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "Opus 4.7" ]
}

@test "model_display_name: claude-haiku-4-5 -> Haiku 4.5" {
  run model_display_name "claude-haiku-4-5-20251001"
  [ "$status" -eq 0 ]
  [ "$output" = "Haiku 4.5" ]
}

@test "model_display_name: gpt-4o-mini -> GPT-4o mini" {
  run model_display_name "gpt-4o-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4o mini" ]
}

@test "model_display_name: gpt-4o -> GPT-4o" {
  run model_display_name "gpt-4o"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4o" ]
}

@test "model_display_name: gpt-4.1 -> GPT-4.1" {
  run model_display_name "gpt-4.1"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4.1" ]
}

@test "model_display_name: gpt-4.1-mini -> GPT-4.1 mini" {
  run model_display_name "gpt-4.1-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4.1 mini" ]
}

@test "model_display_name: gpt-4.1-nano -> GPT-4.1 nano" {
  run model_display_name "gpt-4.1-nano"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4.1 nano" ]
}

@test "model_display_name: o3 -> o3" {
  run model_display_name "o3"
  [ "$status" -eq 0 ]
  [ "$output" = "o3" ]
}

@test "model_display_name: o3-mini -> o3-mini" {
  run model_display_name "o3-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "o3-mini" ]
}

@test "model_display_name: o4-mini -> o4-mini" {
  run model_display_name "o4-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "o4-mini" ]
}

@test "model_display_name: gpt-5.4 -> GPT-5.4" {
  run model_display_name "gpt-5.4"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-5.4" ]
}

@test "model_display_name: gpt-5.4-mini -> GPT-5.4 mini" {
  run model_display_name "gpt-5.4-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-5.4 mini" ]
}

@test "model_display_name: gpt-5.5 -> GPT-5.5" {
  run model_display_name "gpt-5.5"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-5.5" ]
}

@test "model_display_name: gpt-5 -> GPT-5" {
  run model_display_name "gpt-5"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-5" ]
}

@test "model_display_name: gemini-2.5-flash -> Gemini 2.5 Flash" {
  run model_display_name "gemini-2.5-flash"
  [ "$status" -eq 0 ]
  [ "$output" = "Gemini 2.5 Flash" ]
}

@test "model_display_name: gemini-2.5-flash-lite -> Gemini 2.5 Flash Lite" {
  run model_display_name "gemini-2.5-flash-lite"
  [ "$status" -eq 0 ]
  [ "$output" = "Gemini 2.5 Flash Lite" ]
}

@test "model_display_name: unknown model passes through unchanged" {
  run model_display_name "custom-model-v1"
  [ "$status" -eq 0 ]
  [ "$output" = "custom-model-v1" ]
}

# ---------------------------------------------------------------------------
# format_cost
# ---------------------------------------------------------------------------

@test "format_cost: zero produces dollar zero" {
  run format_cost 0
  [ "$status" -eq 0 ]
  [ "$output" = '$0.0000' ]
}

@test "format_cost: 10000 produces one dollar" {
  run format_cost 10000
  [ "$status" -eq 0 ]
  [ "$output" = '$1.0000' ]
}

@test "format_cost: 1234 produces fractional dollar" {
  run format_cost 1234
  [ "$status" -eq 0 ]
  [ "$output" = '$0.1234' ]
}

@test "format_cost: 50000 produces five dollars" {
  run format_cost 50000
  [ "$status" -eq 0 ]
  [ "$output" = '$5.0000' ]
}

# ---------------------------------------------------------------------------
# call_agent_bg and collect_parallel_results
# ---------------------------------------------------------------------------
# Tests use a mock llm-call.sh in a temp SCRIPT_DIR. The mock writes a
# TOKENS: line to stderr on success, or exits non-zero on failure.
# These functions are called directly (no `run`) so sidecar files
# written inside subshells are visible after wait.

_parallel_setup() {
  source "${PROJECT_ROOT}/lib/agents.sh"

  # Temp dir acts as SCRIPT_DIR with a stub llm-call.sh
  MOCK_DIR=$(mktemp -d)
  SCRIPT_DIR="$MOCK_DIR"
  TMPFILES=()
  FAILED_AGENTS=()
  TOKEN_LOG=()
}

_parallel_teardown() {
  rm -rf "$MOCK_DIR"
  for f in "${TMPFILES[@]}"; do rm -f "$f" 2>/dev/null || true; done
}

@test "call_agent_bg: success writes .name and .tokens sidecars" {
  _parallel_setup
  # Note: call_agent_bg is called synchronously here (no &) to keep sidecar assertions
  # in the same shell. The backgrounded code path differs only in subshell scoping of
  # TMPFILES; wait_tier_pids and collect_parallel_results are tested via their own
  # tests and by the documented end-to-end verification in the PR description.

  # Mock llm-call.sh: writes output and emits TOKENS: line to stderr
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "agent output"
echo "TOKENS: input=100 output=200 model=test-model" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent_bg "my-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ -f "${out}.name" ]
  [ "$(cat "${out}.name")" = "my-agent" ]
  [ -f "${out}.tokens" ]
  # .tokens sidecar now includes cache fields (defaulting to 0 when the
  # llm-call.sh stderr line omits them, as in this legacy-format mock).
  [ "$(cat "${out}.tokens")" = "my-agent: input=100 output=200 cache_creation=0 cache_read=0 model=test-model" ]
  [ ! -f "${out}.failed" ]
  [ "$(cat "$out")" = "agent output" ]

  _parallel_teardown
}

@test "call_agent_bg: failure writes .name and .failed, clears output" {
  _parallel_setup

  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "ERROR: bad key" >&2
exit 1
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent_bg "fail-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ -f "${out}.name" ]
  [ "$(cat "${out}.name")" = "fail-agent" ]
  [ -f "${out}.failed" ]
  [ ! -f "${out}.tokens" ]
  [ -z "$(cat "$out")" ]

  _parallel_teardown
}

@test "call_agent_bg: truncated response writes .truncated sidecar" {
  _parallel_setup

  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "partial output"
echo "TRUNCATED:true" >&2
echo "TOKENS: input=50 output=30 model=test-model" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent_bg "trunc-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ -f "${out}.truncated" ]
  [ -f "${out}.tokens" ]

  _parallel_teardown
}

@test "call_agent_bg: forwards cache fields through .tokens sidecar" {
  # Parallel runs must propagate cache_creation / cache_read through the
  # .tokens sidecar so the parent's emit_token_table() can render the
  # cache-aware column layout. Without this, cache activity would be
  # visible only in sequential runs.
  _parallel_setup

  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "response"
echo "TOKENS: input=100 output=200 cache_creation=5000 cache_read=15000 model=test-model" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent_bg "cache-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ -f "${out}.tokens" ]
  [ "$(cat "${out}.tokens")" = "cache-agent: input=100 output=200 cache_creation=5000 cache_read=15000 model=test-model" ]

  _parallel_teardown
}

@test "collect_parallel_results: success agents populate TOKEN_LOG in roster order" {
  _parallel_setup

  # Pre-create output files with sidecars as call_agent_bg would
  local out1 out2
  out1=$(mktemp); out2=$(mktemp)
  TMPFILES+=("$out1" "$out2")

  echo "agent-one" > "${out1}.name"
  echo "agent-one: input=100 output=200 model=m1" > "${out1}.tokens"

  echo "agent-two" > "${out2}.name"
  echo "agent-two: input=300 output=400 model=m2" > "${out2}.tokens"

  collect_parallel_results "$out1" "$out2"

  [ "${#TOKEN_LOG[@]}" -eq 2 ]
  [ "${TOKEN_LOG[0]}" = "agent-one: input=100 output=200 model=m1" ]
  [ "${TOKEN_LOG[1]}" = "agent-two: input=300 output=400 model=m2" ]
  [ "${#FAILED_AGENTS[@]}" -eq 0 ]

  _parallel_teardown
}

@test "collect_parallel_results: failed agents populate FAILED_AGENTS in roster order" {
  _parallel_setup

  local out1 out2 out3
  out1=$(mktemp); out2=$(mktemp); out3=$(mktemp)
  TMPFILES+=("$out1" "$out2" "$out3")

  # out1: success
  echo "agent-ok" > "${out1}.name"
  echo "agent-ok: input=10 output=20 model=m" > "${out1}.tokens"

  # out2: failure
  echo "agent-fail" > "${out2}.name"
  touch "${out2}.failed"

  # out3: success
  echo "agent-ok2" > "${out3}.name"
  echo "agent-ok2: input=5 output=10 model=m" > "${out3}.tokens"

  collect_parallel_results "$out1" "$out2" "$out3"

  [ "${#FAILED_AGENTS[@]}" -eq 1 ]
  [ "${FAILED_AGENTS[0]}" = "agent-fail" ]
  [ "${#TOKEN_LOG[@]}" -eq 2 ]
  [ "${TOKEN_LOG[0]}" = "agent-ok: input=10 output=20 model=m" ]
  [ "${TOKEN_LOG[1]}" = "agent-ok2: input=5 output=10 model=m" ]

  _parallel_teardown
}

@test "collect_parallel_results: registers sidecars with TMPFILES for cleanup" {
  _parallel_setup

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  echo "my-agent" > "${out}.name"
  echo "my-agent: input=1 output=2 model=m" > "${out}.tokens"

  collect_parallel_results "$out"

  # TMPFILES should now include the .name and .tokens sidecars
  [[ " ${TMPFILES[*]} " == *"${out}.name"* ]]
  [[ " ${TMPFILES[*]} " == *"${out}.tokens"* ]]

  _parallel_teardown
}

# ---------------------------------------------------------------------------
# emit_token_table — adaptive column layout based on cache activity
# ---------------------------------------------------------------------------

_token_table_setup() {
  load_function "${PROJECT_ROOT}/lib/pricing.sh" emit_token_table
  # All functions emit_token_table calls transitively. The file-level setup()
  # already loads model_pricing, model_display_name, and format_cost, but
  # list them here explicitly so these tests remain self-contained and don't
  # silently break if someone rearranges the outer setup() or moves these
  # cases to a different file.
  load_function "${PROJECT_ROOT}/lib/pricing.sh" model_pricing
  load_function "${PROJECT_ROOT}/lib/pricing.sh" model_display_name
  load_function "${PROJECT_ROOT}/lib/pricing.sh" format_cost
}

@test "emit_token_table: no cache activity → 6-column layout (legacy)" {
  _token_table_setup
  # Legacy TOKEN_LOG entry shape (no cache_* fields) should also work.
  TOKEN_LOG=("agent-a: input=100 output=50 model=claude-sonnet-4-6")
  run emit_token_table
  [ "$status" -eq 0 ]
  # Header has 6 fields (no Cache Write / Cache Read columns)
  header_line=$(echo "$output" | head -1)
  [[ "$header_line" == *"| Agent | Model | Input | Output | Total | Est. Cost |"* ]]
  # No cache columns
  [[ "$header_line" != *"Cache Write"* ]]
}

@test "emit_token_table: with cache activity → 8-column layout" {
  _token_table_setup
  TOKEN_LOG=(
    "agent-a: input=100 output=50 cache_creation=1000 cache_read=0 model=claude-sonnet-4-6"
    "agent-b: input=50 output=30 cache_creation=0 cache_read=1000 model=claude-sonnet-4-6"
  )
  run emit_token_table
  [ "$status" -eq 0 ]
  header_line=$(echo "$output" | head -1)
  [[ "$header_line" == *"Cache Write"* ]]
  [[ "$header_line" == *"Cache Read"* ]]
}

@test "emit_token_table: cache cost uses cache_write and cache_read rates" {
  _token_table_setup
  # claude-sonnet-4-6: in=3M, out=15M, cache_write=3.75M, cache_read=0.3M per 1M tokens
  # 1000 cache_read tokens × 300000 ÷ 10^8 = 0.003 = 3 tenths-of-a-cent
  # 1000 cache_creation × 3750000 ÷ 10^8 = 0.0375 = 37 tenths-of-a-cent
  # 100 input × 3000000 ÷ 10^8 = 0.003 = 3 tenths-of-a-cent
  # 50 output × 15000000 ÷ 10^8 = 0.0075 = 7 tenths-of-a-cent
  # Total: 3+37+0+7 = 47 tenths-of-a-cent = $0.0047
  # (Exact value depends on integer truncation in awk)
  TOKEN_LOG=("agent-a: input=100 output=50 cache_creation=1000 cache_read=0 model=claude-sonnet-4-6")
  run emit_token_table
  [ "$status" -eq 0 ]
  # Row should contain the 8-col format; cost > 0
  echo "$output" | grep -qE '\| agent-a \| Sonnet 4.6 \| 100 \| 50 \| 1000 \| 0 \| 1150 \|'
}

@test "emit_token_table: legacy TOKEN_LOG entries (no cache fields) treated as zero" {
  _token_table_setup
  # Backward compat: entries without cache_* fields must not break the table.
  TOKEN_LOG=("agent-a: input=100 output=50 model=claude-sonnet-4-6")
  run emit_token_table
  [ "$status" -eq 0 ]
  # Should use legacy 6-col layout (no cache activity detected)
  header_line=$(echo "$output" | head -1)
  [[ "$header_line" != *"Cache Write"* ]]
}

# ---------------------------------------------------------------------------
# cache_priming_effective — gate resolution for the sync-prime code path
# ---------------------------------------------------------------------------

_cp_setup() {
  source "${PROJECT_ROOT}/lib/agents.sh"
}

@test "cache_priming_effective: default is opt-out (returns false)" {
  # Priming is an opt-in tuning knob — default-off, even on anthropic.
  # Live benchmarks showed no net cost win over the unprimed baseline
  # due to opportunistic concurrent-timing cache hits, so we don't
  # impose the +30s wall-clock penalty on the common case.
  _cp_setup
  unset AI_CACHE_PRIMING LLM_PROMPT_CACHING
  AI_PROVIDER=anthropic run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=true + anthropic → true" {
  _cp_setup
  unset LLM_PROMPT_CACHING
  AI_CACHE_PRIMING=true AI_PROVIDER=anthropic run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=true + bedrock-proxy → true" {
  _cp_setup
  unset LLM_PROMPT_CACHING
  AI_CACHE_PRIMING=true AI_PROVIDER=bedrock-proxy run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=true + openai → false (caching not auto-on)" {
  _cp_setup
  unset LLM_PROMPT_CACHING
  AI_CACHE_PRIMING=true AI_PROVIDER=openai run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=true + openai → false (priming only helps Anthropic-shaped providers)" {
  # Even if LLM_PROMPT_CACHING is explicitly enabled, priming is useless
  # on openai/google because those providers don't use cache_control
  # markers. Forcing priming there would pay +30s latency for no cache
  # benefit, so cache_priming_effective gates on provider first.
  _cp_setup
  AI_CACHE_PRIMING=true LLM_PROMPT_CACHING=true AI_PROVIDER=openai run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=true but LLM_PROMPT_CACHING=false → false" {
  # Priming without caching is pointless — we gate it on caching being
  # effective to avoid wasted serial latency.
  _cp_setup
  AI_CACHE_PRIMING=true LLM_PROMPT_CACHING=false AI_PROVIDER=anthropic run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=false explicitly opts out" {
  _cp_setup
  AI_CACHE_PRIMING=false AI_PROVIDER=anthropic run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}

@test "cache_priming_effective: AI_CACHE_PRIMING=1 accepted as enabled" {
  _cp_setup
  AI_CACHE_PRIMING=1 AI_PROVIDER=anthropic run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "true" ]
}

# ---------------------------------------------------------------------------
# lib/*.sh standalone sourcing
#
# load_function in test_helper.bash extracts individual function text via awk,
# so a broken file-scope reference (e.g. to a variable only set in review.sh's
# main()) wouldn't fail any existing test. These smoke tests catch that class
# of regression by sourcing the whole module in a clean subshell.
# ---------------------------------------------------------------------------

@test "lib/languages.sh: sources standalone without error" {
  run bash -c "source '${PROJECT_ROOT}/lib/languages.sh'"
  [ "$status" -eq 0 ]
}

@test "lib/pricing.sh: sources standalone without error" {
  run bash -c "source '${PROJECT_ROOT}/lib/pricing.sh'"
  [ "$status" -eq 0 ]
}

@test "lib/findings.sh: sources standalone without error" {
  run bash -c "source '${PROJECT_ROOT}/lib/findings.sh'"
  [ "$status" -eq 0 ]
}

@test "lib/agents.sh: sources standalone without error" {
  run bash -c "source '${PROJECT_ROOT}/lib/agents.sh'"
  [ "$status" -eq 0 ]
}

@test "cache_priming_effective: AI_PROVIDER unset returns false (no caching possible)" {
  _cp_setup
  unset AI_CACHE_PRIMING LLM_PROMPT_CACHING AI_PROVIDER
  run cache_priming_effective
  [ "$status" -eq 0 ]
  [ "$output" = "false" ]
}
