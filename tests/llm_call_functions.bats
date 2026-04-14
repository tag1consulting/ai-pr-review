#!/usr/bin/env bats
# Tests for pure functions in llm-call.sh.
# Functions are extracted individually at test time — the full script is never
# sourced, so no API calls are made.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/llm-call.sh" is_transient_http
  load_function "${PROJECT_ROOT}/llm-call.sh" is_transient_curl
}

# ---------------------------------------------------------------------------
# is_transient_http
# ---------------------------------------------------------------------------

@test "is_transient_http: 429 is transient" {
  run is_transient_http 429
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 500 is transient" {
  run is_transient_http 500
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 502 is transient" {
  run is_transient_http 502
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 503 is transient" {
  run is_transient_http 503
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 400 is not transient" {
  run is_transient_http 400
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 401 is not transient" {
  run is_transient_http 401
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 403 is not transient" {
  run is_transient_http 403
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 404 is not transient" {
  run is_transient_http 404
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 422 is not transient" {
  run is_transient_http 422
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 200 is not transient" {
  run is_transient_http 200
  [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# is_transient_curl
# ---------------------------------------------------------------------------

@test "is_transient_curl: 7 (connection refused) is transient" {
  run is_transient_curl 7
  [ "$status" -eq 0 ]
}

@test "is_transient_curl: 28 (timeout) is transient" {
  run is_transient_curl 28
  [ "$status" -eq 0 ]
}

@test "is_transient_curl: 56 (network failure) is transient" {
  run is_transient_curl 56
  [ "$status" -eq 0 ]
}

@test "is_transient_curl: 1 (unsupported protocol) is not transient" {
  run is_transient_curl 1
  [ "$status" -eq 1 ]
}

@test "is_transient_curl: 6 (DNS resolution failed) is not transient" {
  run is_transient_curl 6
  [ "$status" -eq 1 ]
}

@test "is_transient_curl: 0 (success) is not transient" {
  run is_transient_curl 0
  [ "$status" -eq 1 ]
}
