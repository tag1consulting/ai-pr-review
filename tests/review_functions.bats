#!/usr/bin/env bats
# Tests for pure functions in review.sh.
# Functions are extracted individually at test time — the full script is never
# sourced, so the orchestration pipeline does not run.

setup() {
  load test_helper
  MODEL_PRICING_FILE="${PROJECT_ROOT}/config/model-pricing.json"
  load_function "${PROJECT_ROOT}/review.sh" detect_language
  load_function "${PROJECT_ROOT}/review.sh" is_test_file
  load_function "${PROJECT_ROOT}/review.sh" model_pricing
  load_function "${PROJECT_ROOT}/review.sh" model_display_name
  load_function "${PROJECT_ROOT}/review.sh" format_cost
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
  [ "$output" = "3000000 15000000" ]
}

@test "model_pricing: claude-opus-4-7 returns correct rates" {
  run model_pricing "claude-opus-4-7"
  [ "$status" -eq 0 ]
  [ "$output" = "5000000 25000000" ]
}

@test "model_pricing: claude-opus-4-6 still matched by Opus pattern" {
  run model_pricing "claude-opus-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "5000000 25000000" ]
}

@test "model_pricing: claude-haiku-4-5 returns correct rates" {
  run model_pricing "claude-haiku-4-5-20251001"
  [ "$status" -eq 0 ]
  [ "$output" = "800000 4000000" ]
}

@test "model_pricing: gpt-4o-mini is not shadowed by gpt-4o pattern" {
  run model_pricing "gpt-4o-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "150000 600000" ]
}

@test "model_pricing: gpt-4o returns correct rates" {
  run model_pricing "gpt-4o"
  [ "$status" -eq 0 ]
  [ "$output" = "2500000 10000000" ]
}

@test "model_pricing: gemini-2.5-flash returns correct rates" {
  run model_pricing "gemini-2.5-flash"
  [ "$status" -eq 0 ]
  [ "$output" = "150000 3500000" ]
}

@test "model_pricing: gemini-2.5-pro returns correct rates" {
  run model_pricing "gemini-2.5-pro"
  [ "$status" -eq 0 ]
  [ "$output" = "1250000 10000000" ]
}

@test "model_pricing: unknown model returns zero rates" {
  run model_pricing "some-unknown-model-v1"
  [ "$status" -eq 0 ]
  [ "$output" = "0 0" ]
}

@test "model_pricing: bedrock-prefixed claude-sonnet matches" {
  run model_pricing "us.anthropic.claude-sonnet-4-6"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000" ]
}

@test "model_pricing: uppercase model name matched case-insensitively" {
  run model_pricing "CLAUDE-SONNET-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000" ]
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

@test "model_display_name: gemini-2.5-flash -> Gemini 2.5 Flash" {
  run model_display_name "gemini-2.5-flash"
  [ "$status" -eq 0 ]
  [ "$output" = "Gemini 2.5 Flash" ]
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
  load_function "${PROJECT_ROOT}/review.sh" call_agent_bg
  load_function "${PROJECT_ROOT}/review.sh" collect_parallel_results

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
  [ "$(cat "${out}.tokens")" = "my-agent: input=100 output=200 model=test-model" ]
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
