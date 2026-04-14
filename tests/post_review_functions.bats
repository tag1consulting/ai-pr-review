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
