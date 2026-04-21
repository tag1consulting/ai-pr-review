#!/usr/bin/env bats
# Tests for the sequential call_agent function in review.sh.
# A mock llm-call.sh is written into a temp SCRIPT_DIR to control
# provider behavior without real API calls.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/review.sh" call_agent
  load_function "${PROJECT_ROOT}/review.sh" mktemp_tracked

  MOCK_DIR=$(mktemp -d)
  SCRIPT_DIR="$MOCK_DIR"
  TMPFILES=()
  FAILED_AGENTS=()
  TOKEN_LOG=()
}

teardown() {
  rm -rf "$MOCK_DIR"
  for f in "${TMPFILES[@]}"; do rm -f "$f" 2>/dev/null || true; done
}

# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

@test "call_agent: success writes output and appends TOKEN_LOG" {
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "agent response"
echo "TOKENS: input=100 output=200 model=test-model" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent "my-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ "$(cat "$out")" = "agent response" ]
  [ "${#TOKEN_LOG[@]}" -eq 1 ]
  [ "${TOKEN_LOG[0]}" = "my-agent: input=100 output=200 model=test-model" ]
  [ "${#FAILED_AGENTS[@]}" -eq 0 ]
}

@test "call_agent: success with custom max_tokens passes it to llm-call.sh" {
  # Stub captures its arguments so we can assert max_tokens was forwarded
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "args: $*" > /tmp/llm-call-args-$$.txt
echo "response"
echo "TOKENS: input=1 output=1 model=m" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent "my-agent" "test-model" "/dev/null" "/dev/null" "$out" "8192"

  # The 4th positional to llm-call.sh is max_tokens
  grep -q "8192" /tmp/llm-call-args-$$.txt 2>/dev/null && rm -f /tmp/llm-call-args-$$.txt
  [ "${#FAILED_AGENTS[@]}" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

@test "call_agent: exit 1 from llm-call.sh adds to FAILED_AGENTS and clears output" {
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "ERROR: bad API key" >&2
exit 1
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  echo "old content" > "$out"
  TMPFILES+=("$out")

  call_agent "fail-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ "${#FAILED_AGENTS[@]}" -eq 1 ]
  [ "${FAILED_AGENTS[0]}" = "fail-agent" ]
  # Output is emptied on failure
  [ "$(cat "$out")" = "" ]
  [ "${#TOKEN_LOG[@]}" -eq 0 ]
}

@test "call_agent: exit 2 (transient) adds to FAILED_AGENTS with transient failure type" {
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "ERROR: all retries exhausted" >&2
exit 2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent "transient-agent" "test-model" "/dev/null" "/dev/null" "$out" 2>&1

  [ "${#FAILED_AGENTS[@]}" -eq 1 ]
  [ "${FAILED_AGENTS[0]}" = "transient-agent" ]
}

@test "call_agent: exit 3 (content filter) adds to FAILED_AGENTS" {
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "ERROR: blocked by safety filter" >&2
exit 3
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent "blocked-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ "${#FAILED_AGENTS[@]}" -eq 1 ]
  [ "${FAILED_AGENTS[0]}" = "blocked-agent" ]
}

# ---------------------------------------------------------------------------
# Truncation sidecar
# ---------------------------------------------------------------------------

@test "call_agent: TRUNCATED:true in stderr writes .truncated sidecar" {
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "partial"
echo "TRUNCATED:true" >&2
echo "TOKENS: input=50 output=30 model=m" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent "trunc-agent" "test-model" "/dev/null" "/dev/null" "$out"

  [ -f "${out}.truncated" ]
  rm -f "${out}.truncated"
}

# ---------------------------------------------------------------------------
# Token log accumulation across multiple calls
# ---------------------------------------------------------------------------

@test "call_agent: multiple successful calls accumulate TOKEN_LOG entries" {
  cat > "${MOCK_DIR}/llm-call.sh" <<'EOF'
#!/usr/bin/env bash
echo "ok"
echo "TOKENS: input=10 output=20 model=m" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out1 out2
  out1=$(mktemp); out2=$(mktemp)
  TMPFILES+=("$out1" "$out2")

  call_agent "agent-one" "test-model" "/dev/null" "/dev/null" "$out1"
  call_agent "agent-two" "test-model" "/dev/null" "/dev/null" "$out2"

  [ "${#TOKEN_LOG[@]}" -eq 2 ]
  [ "${TOKEN_LOG[0]}" = "agent-one: input=10 output=20 model=m" ]
  [ "${TOKEN_LOG[1]}" = "agent-two: input=10 output=20 model=m" ]
}
