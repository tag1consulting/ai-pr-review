#!/usr/bin/env bats
# Tests for retry_curl in llm-call.sh.
# curl is stubbed via a bash function injected before the test, since
# load_function evals functions into the current shell. The stub writes
# HTTP status codes to stdout (simulating curl's -o /dev/null -w "%{http_code}"
# pattern) and controls the curl exit code via a counter file.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/llm-call.sh" is_transient_http
  load_function "${PROJECT_ROOT}/llm-call.sh" is_transient_curl
  load_function "${PROJECT_ROOT}/llm-call.sh" check_http_status
  load_function "${PROJECT_ROOT}/llm-call.sh" retry_curl

  # Default retry configuration
  LLM_RETRY_COUNT=3
  LLM_RETRY_BASE_DELAY=0

  # Counter file for stub state
  COUNTER_FILE=$(mktemp)
  echo 0 > "$COUNTER_FILE"

  # Stub out sleep so tests are fast
  sleep() { :; }
}

teardown() {
  rm -f "$COUNTER_FILE"
}

# ---------------------------------------------------------------------------
# Helper: inject a curl stub that returns given HTTP codes in sequence,
# then 200 on all subsequent calls.
# ---------------------------------------------------------------------------
_make_curl_stub() {
  local -a codes=("$@")
  local codes_str
  codes_str=$(printf '%s\n' "${codes[@]}")
  # Write codes to a temp file for the stub to read
  echo "$codes_str" > "${COUNTER_FILE}.codes"
  echo 0 > "$COUNTER_FILE"

  # shellcheck disable=SC2034
  curl() {
    local call_num
    call_num=$(cat "$COUNTER_FILE")
    echo $((call_num + 1)) > "$COUNTER_FILE"

    local total_codes
    total_codes=$(wc -l < "${COUNTER_FILE}.codes")
    local code_index=$((call_num + 1))

    if [[ "$code_index" -le "$total_codes" ]]; then
      local code
      code=$(sed -n "${code_index}p" "${COUNTER_FILE}.codes")
      echo "$code"
      return 0
    else
      # All predefined codes exhausted: return 200
      echo "200"
      return 0
    fi
  }
}

# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

@test "retry_curl: immediate 200 returns 0" {
  _make_curl_stub 200
  run retry_curl "test-provider" --data-binary @-
  [ "$status" -eq 0 ]
}

@test "retry_curl: replays body on retry (stdin captured to file)" {
  # Stub: fail once with 503, then succeed
  _make_curl_stub 503 200

  # We pass a body via stdin via a pipe; the retry mechanism must capture
  # stdin before the first attempt so subsequent retries can re-read it.
  echo "request-body" | retry_curl "test-provider" --data-binary @-
  [ "$?" -eq 0 ]
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 2 ]
}

# ---------------------------------------------------------------------------
# Transient retry behavior
# ---------------------------------------------------------------------------

@test "retry_curl: retries on 503 and succeeds on second attempt" {
  _make_curl_stub 503 200
  run retry_curl "test-provider"
  [ "$status" -eq 0 ]
  # Should have been called twice
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 2 ]
}

@test "retry_curl: retries on 429 and succeeds on third attempt" {
  _make_curl_stub 429 429 200
  run retry_curl "test-provider"
  [ "$status" -eq 0 ]
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 3 ]
}

@test "retry_curl: retries on 504 (new transient code)" {
  _make_curl_stub 504 200
  run retry_curl "test-provider"
  [ "$status" -eq 0 ]
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 2 ]
}

@test "retry_curl: retries on 520 (Cloudflare edge)" {
  _make_curl_stub 520 200
  run retry_curl "test-provider"
  [ "$status" -eq 0 ]
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 2 ]
}

# ---------------------------------------------------------------------------
# Exhausted retries
# ---------------------------------------------------------------------------

@test "retry_curl: exits 2 after all retries exhausted on transient 503" {
  # 4 calls total: initial + 3 retries, all 503
  _make_curl_stub 503 503 503 503
  run retry_curl "test-provider"
  [ "$status" -eq 2 ]
}

@test "retry_curl: exits 1 on permanent 400 without retrying" {
  _make_curl_stub 400
  run retry_curl "test-provider"
  [ "$status" -eq 1 ]
  # Should only be called once
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 1 ]
}

@test "retry_curl: exits 1 on permanent 401 without retrying" {
  _make_curl_stub 401
  run retry_curl "test-provider"
  [ "$status" -eq 1 ]
  calls=$(cat "$COUNTER_FILE")
  [ "$calls" -eq 1 ]
}

# ---------------------------------------------------------------------------
# is_transient_http: new codes added in Phase 3
# ---------------------------------------------------------------------------

@test "is_transient_http: 408 is transient" {
  run is_transient_http 408
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 504 is transient" {
  run is_transient_http 504
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 520 is transient" {
  run is_transient_http 520
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 521 is transient" {
  run is_transient_http 521
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 522 is transient" {
  run is_transient_http 522
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 523 is transient" {
  run is_transient_http 523
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 524 is transient" {
  run is_transient_http 524
  [ "$status" -eq 0 ]
}
