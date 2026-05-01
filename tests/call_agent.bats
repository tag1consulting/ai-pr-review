#!/usr/bin/env bats
# Tests for the sequential call_agent function in review.sh.
# A mock llm-call.sh is written into a temp SCRIPT_DIR to control
# provider behavior without real API calls.
bats_require_minimum_version 1.5.0

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/review.sh" call_agent
  load_function "${PROJECT_ROOT}/review.sh" mktemp_tracked
  load_function "${PROJECT_ROOT}/review.sh" effective_prompt

  MOCK_DIR=$(mktemp -d)
  SCRIPT_DIR="$MOCK_DIR"
  # effective_prompt() uses this prefix for combined-prompt temp files; the
  # parent review.sh sets it at script top and cleans matching files on exit.
  EFFECTIVE_PROMPT_PREFIX="${MOCK_DIR}/ai-review-prompt-$$"
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
  # Stub captures its arguments to a known path under MOCK_DIR.
  # Use an unquoted heredoc so the path expands at write time (not at
  # stub execution time, which would give a different PID).
  local args_file="${MOCK_DIR}/llm-call-args.txt"
  cat > "${MOCK_DIR}/llm-call.sh" <<EOF
#!/usr/bin/env bash
echo "args: \$*" > "${args_file}"
echo "response"
echo "TOKENS: input=1 output=1 model=m" >&2
EOF
  chmod +x "${MOCK_DIR}/llm-call.sh"

  local out
  out=$(mktemp)
  TMPFILES+=("$out")

  call_agent "my-agent" "test-model" "/dev/null" "/dev/null" "$out" "8192"

  # The 4th positional to llm-call.sh is max_tokens — assert it was forwarded
  run grep -q "8192" "$args_file"
  [ "$status" -eq 0 ]
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

# ---------------------------------------------------------------------------
# effective_prompt — conditionally appends the suggestion addendum to the
# system prompt for agents that support code suggestions.
# ---------------------------------------------------------------------------

@test "effective_prompt: returns base prompt unchanged when suggestions disabled" {
  local base
  base=$(mktemp); TMPFILES+=("$base")
  echo "base prompt content" > "$base"

  unset AI_ENABLE_SUGGESTIONS
  run effective_prompt "code-reviewer" "$base"
  [ "$status" -eq 0 ]
  [ "$output" = "$base" ]
}

@test "effective_prompt: returns base prompt unchanged when AI_ENABLE_SUGGESTIONS=false" {
  local base
  base=$(mktemp); TMPFILES+=("$base")
  echo "base prompt content" > "$base"

  AI_ENABLE_SUGGESTIONS=false run effective_prompt "code-reviewer" "$base"
  [ "$status" -eq 0 ]
  [ "$output" = "$base" ]
}

@test "effective_prompt: returns base prompt for agents that do NOT support suggestions" {
  local base
  base=$(mktemp); TMPFILES+=("$base")
  echo "base prompt content" > "$base"

  for agent in pr-summarizer architecture-reviewer adversarial-general; do
    AI_ENABLE_SUGGESTIONS=true run effective_prompt "$agent" "$base"
    [ "$status" -eq 0 ]
    [ "$output" = "$base" ]
  done
}

@test "effective_prompt: appends addendum for eligible agents when enabled" {
  # Set up a fake SCRIPT_DIR with a prompts/ subdir containing the addendum
  mkdir -p "${MOCK_DIR}/prompts"
  echo "ADDENDUM CONTENT" > "${MOCK_DIR}/prompts/suggestion-addendum.md"

  local base
  base=$(mktemp); TMPFILES+=("$base")
  echo "BASE CONTENT" > "$base"

  for agent in code-reviewer edge-case-hunter security-reviewer silent-failure-hunter blind-hunter; do
    AI_ENABLE_SUGGESTIONS=true run effective_prompt "$agent" "$base"
    [ "$status" -eq 0 ]
    # Output is a path to a new file combining base + addendum
    [ "$output" != "$base" ]
    [ -f "$output" ]
    TMPFILES+=("$output")
    grep -q "BASE CONTENT" "$output"
    grep -q "ADDENDUM CONTENT" "$output"
  done
}

@test "effective_prompt: falls back to base when addendum file is missing" {
  # No prompts/ subdir created — addendum does not exist
  local base
  base=$(mktemp); TMPFILES+=("$base")
  echo "BASE CONTENT" > "$base"

  AI_ENABLE_SUGGESTIONS=true run --separate-stderr effective_prompt "code-reviewer" "$base"
  [ "$status" -eq 0 ]
  [ "$output" = "$base" ]
  [[ "$stderr" == *"Suggestion addendum missing"* ]]
}

@test "effective_prompt: falls back to base when base prompt is missing" {
  mkdir -p "${MOCK_DIR}/prompts"
  echo "ADDENDUM CONTENT" > "${MOCK_DIR}/prompts/suggestion-addendum.md"

  AI_ENABLE_SUGGESTIONS=true run --separate-stderr effective_prompt "code-reviewer" "/nonexistent/prompt.md"
  [ "$status" -eq 0 ]
  [ "$output" = "/nonexistent/prompt.md" ]
  [[ "$stderr" == *"Base prompt missing"* ]]
}

@test "effective_prompt: accepts TRUE and True as enabled (case-insensitive)" {
  mkdir -p "${MOCK_DIR}/prompts"
  echo "ADDENDUM CONTENT" > "${MOCK_DIR}/prompts/suggestion-addendum.md"

  local base
  base=$(mktemp); TMPFILES+=("$base")
  echo "BASE CONTENT" > "$base"

  for val in TRUE True tRuE; do
    AI_ENABLE_SUGGESTIONS="$val" run effective_prompt "code-reviewer" "$base"
    [ "$status" -eq 0 ]
    [ "$output" != "$base" ]
    [ -f "$output" ]
    TMPFILES+=("$output")
    grep -q "ADDENDUM CONTENT" "$output"
  done
}
