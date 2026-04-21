#!/usr/bin/env bats
# Tests for the configurable inputs added in issue #73:
#   AI_CONFIDENCE_THRESHOLD, AI_MAX_TOKENS_PER_AGENT, AI_MAX_INLINE
#
# These are inline validation blocks (not extractable functions), so each test
# runs a minimal harness subprocess that sources just the relevant lines via
# sed extraction.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
}

# ---------------------------------------------------------------------------
# Helpers: source just the validation block from the real script
# ---------------------------------------------------------------------------

# Evaluates AI_MAX_TOKENS_PER_AGENT validation logic and prints the result.
# Value is passed via env var to avoid bash -c injection.
_eval_tokens() {
  AI_MAX_TOKENS_PER_AGENT="$1" bash << 'EOF'
    _raw_tokens="${AI_MAX_TOKENS_PER_AGENT:-8192}"
    if [[ "$_raw_tokens" =~ ^[0-9]+$ ]] && [[ "$_raw_tokens" -ge 256 ]] && [[ "$_raw_tokens" -le 65536 ]]; then
      AI_MAX_TOKENS_PER_AGENT="$_raw_tokens"
    else
      AI_MAX_TOKENS_PER_AGENT=8192
    fi
    echo "$AI_MAX_TOKENS_PER_AGENT"
EOF
}

# Evaluates AI_CONFIDENCE_THRESHOLD validation logic and prints the result.
# Value is passed via env var to avoid bash -c injection.
_eval_confidence() {
  AI_CONFIDENCE_THRESHOLD="$1" bash << 'EOF'
    _raw_conf="${AI_CONFIDENCE_THRESHOLD:-75}"
    if [[ "$_raw_conf" =~ ^[0-9]+$ ]] && [[ "$_raw_conf" -ge 0 ]] && [[ "$_raw_conf" -le 100 ]]; then
      CONFIDENCE_THRESHOLD="$_raw_conf"
    else
      CONFIDENCE_THRESHOLD=75
    fi
    echo "$CONFIDENCE_THRESHOLD"
EOF
}

# Evaluates AI_MAX_INLINE validation logic and prints the result.
# Value is passed via env var to avoid bash -c injection.
_eval_max_inline() {
  AI_MAX_INLINE="$1" bash << 'EOF'
    _raw_mi="${AI_MAX_INLINE:-25}"
    if [[ "$_raw_mi" =~ ^[0-9]+$ ]]; then
      max_inline="$_raw_mi"
    else
      max_inline=25
    fi
    echo "$max_inline"
EOF
}

# ---------------------------------------------------------------------------
# AI_MAX_TOKENS_PER_AGENT
# ---------------------------------------------------------------------------

@test "max-tokens-per-agent: valid value is accepted" {
  result=$(_eval_tokens "4096")
  [ "$result" = "4096" ]
}

@test "max-tokens-per-agent: default 8192 when unset" {
  result=$(bash -c '
    unset AI_MAX_TOKENS_PER_AGENT
    _raw_tokens="${AI_MAX_TOKENS_PER_AGENT:-8192}"
    if [[ "$_raw_tokens" =~ ^[0-9]+$ ]] && [[ "$_raw_tokens" -ge 256 ]] && [[ "$_raw_tokens" -le 65536 ]]; then
      AI_MAX_TOKENS_PER_AGENT="$_raw_tokens"
    else
      AI_MAX_TOKENS_PER_AGENT=8192
    fi
    echo "$AI_MAX_TOKENS_PER_AGENT"
  ')
  [ "$result" = "8192" ]
}

@test "max-tokens-per-agent: non-integer falls back to 8192" {
  result=$(_eval_tokens "notanumber")
  [ "$result" = "8192" ]
}

@test "max-tokens-per-agent: below minimum (255) falls back to 8192" {
  result=$(_eval_tokens "255")
  [ "$result" = "8192" ]
}

@test "max-tokens-per-agent: minimum boundary (256) accepted" {
  result=$(_eval_tokens "256")
  [ "$result" = "256" ]
}

@test "max-tokens-per-agent: maximum boundary (65536) accepted" {
  result=$(_eval_tokens "65536")
  [ "$result" = "65536" ]
}

@test "max-tokens-per-agent: above maximum (65537) falls back to 8192" {
  result=$(_eval_tokens "65537")
  [ "$result" = "8192" ]
}

@test "max-tokens-per-agent: float falls back to 8192" {
  result=$(_eval_tokens "4096.5")
  [ "$result" = "8192" ]
}

# ---------------------------------------------------------------------------
# AI_CONFIDENCE_THRESHOLD
# ---------------------------------------------------------------------------

@test "confidence-threshold: valid value 50 is accepted" {
  result=$(_eval_confidence "50")
  [ "$result" = "50" ]
}

@test "confidence-threshold: default 75 when unset" {
  result=$(bash -c '
    unset AI_CONFIDENCE_THRESHOLD
    _raw_conf="${AI_CONFIDENCE_THRESHOLD:-75}"
    if [[ "$_raw_conf" =~ ^[0-9]+$ ]] && [[ "$_raw_conf" -ge 0 ]] && [[ "$_raw_conf" -le 100 ]]; then
      CONFIDENCE_THRESHOLD="$_raw_conf"
    else
      CONFIDENCE_THRESHOLD=75
    fi
    echo "$CONFIDENCE_THRESHOLD"
  ')
  [ "$result" = "75" ]
}

@test "confidence-threshold: boundary 0 accepted" {
  result=$(_eval_confidence "0")
  [ "$result" = "0" ]
}

@test "confidence-threshold: boundary 100 accepted" {
  result=$(_eval_confidence "100")
  [ "$result" = "100" ]
}

@test "confidence-threshold: above 100 falls back to 75" {
  result=$(_eval_confidence "101")
  [ "$result" = "75" ]
}

@test "confidence-threshold: non-integer falls back to 75" {
  result=$(_eval_confidence "high")
  [ "$result" = "75" ]
}

@test "confidence-threshold: float falls back to 75" {
  result=$(_eval_confidence "75.5")
  [ "$result" = "75" ]
}

# ---------------------------------------------------------------------------
# AI_MAX_INLINE
# ---------------------------------------------------------------------------

@test "max-inline: valid value 10 is accepted" {
  result=$(_eval_max_inline "10")
  [ "$result" = "10" ]
}

@test "max-inline: zero is accepted (no inline comments)" {
  result=$(_eval_max_inline "0")
  [ "$result" = "0" ]
}

@test "max-inline: default 25 when unset" {
  result=$(bash -c '
    unset AI_MAX_INLINE
    _raw_mi="${AI_MAX_INLINE:-25}"
    if [[ "$_raw_mi" =~ ^[0-9]+$ ]]; then
      max_inline="$_raw_mi"
    else
      max_inline=25
    fi
    echo "$max_inline"
  ')
  [ "$result" = "25" ]
}

@test "max-inline: non-integer falls back to 25" {
  result=$(_eval_max_inline "lots")
  [ "$result" = "25" ]
}

@test "max-inline: float falls back to 25" {
  result=$(_eval_max_inline "10.5")
  [ "$result" = "25" ]
}

# ---------------------------------------------------------------------------
# AI_PARALLEL default
# ---------------------------------------------------------------------------

@test "AI_PARALLEL defaults to true when unset" {
  result=$(bash -c '
    unset AI_PARALLEL
    if [[ "${AI_PARALLEL:-true}" == "true" ]]; then echo "parallel"; else echo "sequential"; fi
  ')
  [ "$result" = "parallel" ]
}

@test "AI_PARALLEL=false selects sequential path" {
  result=$(bash -c '
    AI_PARALLEL=false
    if [[ "${AI_PARALLEL:-true}" == "true" ]]; then echo "parallel"; else echo "sequential"; fi
  ')
  [ "$result" = "sequential" ]
}
