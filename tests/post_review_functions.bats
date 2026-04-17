#!/usr/bin/env bats
# Tests for pure functions in post-review.sh.
# Functions are extracted individually at test time — the full script is never
# sourced, so the GitHub posting pipeline does not run.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/post-review.sh" severity_icon
}

# ---------------------------------------------------------------------------
# severity_icon
# ---------------------------------------------------------------------------

@test "severity_icon: critical -> cross mark" {
  run severity_icon "critical"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "severity_icon: Critical (mixed case) -> cross mark" {
  run severity_icon "Critical"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "severity_icon: high -> siren" {
  run severity_icon "high"
  [ "$status" -eq 0 ]
  [ "$output" = "🚨" ]
}

@test "severity_icon: High (mixed case) -> siren" {
  run severity_icon "High"
  [ "$status" -eq 0 ]
  [ "$output" = "🚨" ]
}

@test "severity_icon: medium -> orange diamond" {
  run severity_icon "medium"
  [ "$status" -eq 0 ]
  [ "$output" = "🔶" ]
}

@test "severity_icon: low -> speech bubble" {
  run severity_icon "low"
  [ "$status" -eq 0 ]
  [ "$output" = "💬" ]
}

@test "severity_icon: unknown severity -> white circle" {
  run severity_icon "info"
  [ "$status" -eq 0 ]
  [ "$output" = "⚪" ]
}

@test "severity_icon: empty string -> white circle" {
  run severity_icon ""
  [ "$status" -eq 0 ]
  [ "$output" = "⚪" ]
}

@test "severity_icon: CRITICAL (all caps) -> cross mark" {
  run severity_icon "CRITICAL"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "severity_icon: HIGH (all caps) -> siren" {
  run severity_icon "HIGH"
  [ "$status" -eq 0 ]
  [ "$output" = "🚨" ]
}

# ---------------------------------------------------------------------------
# gh_api_retry — structural tests (no real API calls)
# ---------------------------------------------------------------------------

@test "gh_api_retry: function is defined and callable" {
  load_function "${PROJECT_ROOT}/post-review.sh" gh_api_retry
  # Just verify the function exists (no actual API call)
  declare -f gh_api_retry > /dev/null
}

# ---------------------------------------------------------------------------
# truncate_body — byte-count aware truncation
# ---------------------------------------------------------------------------

setup_truncate_body() {
  load_function "${PROJECT_ROOT}/post-review.sh" truncate_body
  # truncate_body references the module-level MAX_BODY_SIZE constant.
  MAX_BODY_SIZE=64000
}

@test "truncate_body: short ASCII body returned unchanged" {
  setup_truncate_body
  run truncate_body "hello world"
  [ "$status" -eq 0 ]
  [ "$output" = "hello world" ]
}

@test "truncate_body: body at exactly the byte limit is returned unchanged" {
  setup_truncate_body
  local body
  body=$(printf 'a%.0s' $(seq 1 64000))
  run truncate_body "$body"
  [ "$status" -eq 0 ]
  [ "${#output}" -eq 64000 ]
}

@test "truncate_body: ASCII body over limit is truncated with notice" {
  setup_truncate_body
  local body
  body=$(printf 'a%.0s' $(seq 1 70000))
  run truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  # Truncated body plus notice must stay under the 65,536-byte API limit.
  local byte_len
  byte_len=$(printf '%s' "$output" | wc -c)
  [ "$byte_len" -lt 65536 ]
}

@test "truncate_body: multi-byte UTF-8 truncation produces valid UTF-8" {
  setup_truncate_body
  # 30,000 copies of a 3-byte character = 90,000 bytes. Truncation lands
  # inside a multi-byte sequence; iconv drops the partial codepoint so
  # output stays valid UTF-8.
  local body
  body=$(printf '日%.0s' $(seq 1 30000))
  run truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  # Round-trip through iconv: valid UTF-8 passes through byte-identical.
  local original_len roundtrip_len
  original_len=$(printf '%s' "$output" | wc -c)
  roundtrip_len=$(printf '%s' "$output" | iconv -f UTF-8 -t UTF-8 2>/dev/null | wc -c)
  [ "$original_len" -eq "$roundtrip_len" ]
}

@test "truncate_body: 50k multi-byte chars (150kB) triggers truncation" {
  # Under the old character-based check, 50,000 chars <= 64,000 would pass
  # through unmodified even though the byte length (150,000) exceeds
  # GitHub's 65,536-byte API limit. Byte-count aware check correctly
  # truncates this.
  setup_truncate_body
  local body
  body=$(printf '日%.0s' $(seq 1 50000))
  run truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
}
