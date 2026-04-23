#!/usr/bin/env bats
# Tests for pure functions in post-review-bitbucket.sh.
#
# Covers the three helpers duplicated from post-review.sh (severity_icon,
# format_source_tag, truncate_body) plus a parity test that asserts both
# implementations produce identical output for a shared fixture — this is
# the drift-detection tripwire called out in the sibling-script design.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/post-review-bitbucket.sh" severity_icon
  load_function "${PROJECT_ROOT}/post-review-bitbucket.sh" format_source_tag
  # truncate_body depends on MAX_BODY_SIZE; load both under prefixed names
  # so we can exercise the Bitbucket cap (32000) separately from GitHub's.
}

# ---------------------------------------------------------------------------
# severity_icon — identical mapping to GitHub; verify independently
# ---------------------------------------------------------------------------

@test "bitbucket severity_icon: critical -> cross mark" {
  run severity_icon "critical"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "bitbucket severity_icon: Critical (mixed case) -> cross mark" {
  run severity_icon "Critical"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "bitbucket severity_icon: high -> siren" {
  run severity_icon "high"
  [ "$output" = "🚨" ]
}

@test "bitbucket severity_icon: medium -> orange diamond" {
  run severity_icon "medium"
  [ "$output" = "🔶" ]
}

@test "bitbucket severity_icon: low -> speech bubble" {
  run severity_icon "low"
  [ "$output" = "💬" ]
}

@test "bitbucket severity_icon: unknown -> white circle" {
  run severity_icon "info"
  [ "$output" = "⚪" ]
}

# ---------------------------------------------------------------------------
# format_source_tag
# ---------------------------------------------------------------------------

@test "bitbucket format_source_tag: single source" {
  run format_source_tag '{"source":"code-reviewer"}'
  [ "$status" -eq 0 ]
  [ "$output" = "[code-reviewer]" ]
}

@test "bitbucket format_source_tag: sources array with one entry" {
  run format_source_tag '{"sources":["shellcheck"]}'
  [ "$output" = "[shellcheck]" ]
}

@test "bitbucket format_source_tag: multiple sources" {
  run format_source_tag '{"sources":["code-reviewer","semgrep","shellcheck"]}'
  [ "$output" = "[code-reviewer] *(also flagged by: semgrep,shellcheck)*" ]
}

@test "bitbucket format_source_tag: missing both fields -> unknown" {
  run format_source_tag '{}'
  [ "$output" = "[unknown]" ]
}

# ---------------------------------------------------------------------------
# truncate_body — Bitbucket uses MAX_BODY_SIZE=32000 (vs GitHub's 64000)
# ---------------------------------------------------------------------------

@test "bitbucket truncate_body: short body is returned as-is" {
  # Source the Bitbucket truncate_body inline so MAX_BODY_SIZE=32000 is in scope.
  # shellcheck disable=SC2030,SC2031
  MAX_BODY_SIZE=32000
  load_function "${PROJECT_ROOT}/post-review-bitbucket.sh" truncate_body
  run truncate_body "hello world"
  [ "$status" -eq 0 ]
  [ "$output" = "hello world" ]
}

@test "bitbucket truncate_body: body over 32000 bytes is truncated with notice" {
  MAX_BODY_SIZE=32000
  load_function "${PROJECT_ROOT}/post-review-bitbucket.sh" truncate_body
  local long
  long=$(printf 'a%.0s' {1..35000})
  run truncate_body "$long"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  [[ "$output" == *"Bitbucket Cloud"* ]]
  # The truncated prefix should be at most MAX_BODY_SIZE bytes
  local out_bytes
  out_bytes=$(printf '%s' "$output" | wc -c)
  # Allow for the truncation notice footer (~160 bytes)
  [ "$out_bytes" -lt 32500 ]
}

@test "bitbucket truncate_body: exactly at cap is returned as-is" {
  MAX_BODY_SIZE=32000
  load_function "${PROJECT_ROOT}/post-review-bitbucket.sh" truncate_body
  local at_cap
  at_cap=$(printf 'a%.0s' {1..32000})
  run truncate_body "$at_cap"
  [ "$status" -eq 0 ]
  [[ "$output" != *"truncated"* ]]
}

# ---------------------------------------------------------------------------
# Parity: severity_icon is defined identically in both scripts.
# Drift-detection tripwire — fails if one implementation updates without
# the other.
# ---------------------------------------------------------------------------

@test "parity: severity_icon produces identical output in both scripts" {
  # Fresh shell per run to avoid function collision.
  local inputs=("critical" "Critical" "high" "HIGH" "medium" "Medium" "low" "LOW" "info" "")
  for input in "${inputs[@]}"; do
    local gh_out bb_out
    gh_out=$(bash -c "$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh"); severity_icon '${input}'")
    bb_out=$(bash -c "$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh"); severity_icon '${input}'")
    [ "$gh_out" = "$bb_out" ] || { echo "drift on input='${input}': gh='${gh_out}' bb='${bb_out}'" >&2; return 1; }
  done
}

@test "parity: format_source_tag produces identical output in both scripts" {
  local fixtures=(
    '{"source":"code-reviewer"}'
    '{"sources":["a","b","c"]}'
    '{"sources":[]}'
    '{}'
    '{"source":"","sources":[]}'
  )
  for fixture in "${fixtures[@]}"; do
    local gh_out bb_out
    gh_out=$(bash -c "$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh"); format_source_tag '${fixture}'")
    bb_out=$(bash -c "$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh"); format_source_tag '${fixture}'")
    [ "$gh_out" = "$bb_out" ] || { echo "drift on fixture='${fixture}': gh='${gh_out}' bb='${bb_out}'" >&2; return 1; }
  done
}
