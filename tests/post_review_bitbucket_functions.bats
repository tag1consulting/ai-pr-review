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
  # Output must be returned byte-for-byte unchanged.
  local out_bytes
  out_bytes=$(printf '%s' "$output" | wc -c)
  [ "$out_bytes" -eq 32000 ]
}

@test "bitbucket truncate_body: multi-byte UTF-8 body truncated to valid UTF-8" {
  MAX_BODY_SIZE=32000
  load_function "${PROJECT_ROOT}/post-review-bitbucket.sh" truncate_body
  # '日' is 3 bytes; 11000 repetitions = 33000 bytes (over cap).
  # head -c 32000 cuts mid-codepoint at byte 32000; iconv strips the partial
  # codepoint so the output must round-trip as valid UTF-8.
  local long_utf8
  long_utf8=$(printf '日%.0s' {1..11000})
  run truncate_body "$long_utf8"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  # Verify the truncated prefix is valid UTF-8 (iconv exits 0 on clean input).
  printf '%s' "$output" | iconv -f UTF-8 -t UTF-8 > /dev/null
}

# ---------------------------------------------------------------------------
# Parity: severity_icon is defined identically in both scripts.
# Drift-detection tripwire — fails if one implementation updates without
# the other.
# ---------------------------------------------------------------------------

@test "parity: severity_icon produces identical output in both scripts" {
  # Fresh shell per run to avoid function collision.
  # Fixtures are passed as positional args ($1), not interpolated into the
  # bash -c script string, so embedded quotes / metacharacters cannot break
  # quoting or execute unintended commands.
  local inputs=("critical" "Critical" "high" "HIGH" "medium" "Medium" "low" "LOW" "info" "")
  local gh_fn bb_fn
  gh_fn=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  for input in "${inputs[@]}"; do
    local gh_out bb_out
    gh_out=$(bash -c "${gh_fn}"'; severity_icon "$1"' _ "$input")
    bb_out=$(bash -c "${bb_fn}"'; severity_icon "$1"' _ "$input")
    [ "$gh_out" = "$bb_out" ] || { echo "drift on input='${input}': gh='${gh_out}' bb='${bb_out}'" >&2; return 1; }
  done
}

@test "parity: format_source_tag produces identical output in both scripts" {
  # Fixtures are passed as positional args ($1), not interpolated (see note
  # above). This matters because fixtures contain single quotes and braces.
  local fixtures=(
    '{"source":"code-reviewer"}'
    '{"sources":["a","b","c"]}'
    '{"sources":[]}'
    '{}'
    '{"source":"","sources":[]}'
    '{"source":"agent-a","sources":["agent-a"]}'
  )
  local gh_fn bb_fn
  gh_fn=$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  for fixture in "${fixtures[@]}"; do
    local gh_out bb_out
    gh_out=$(bash -c "${gh_fn}"'; format_source_tag "$1"' _ "$fixture")
    bb_out=$(bash -c "${bb_fn}"'; format_source_tag "$1"' _ "$fixture")
    [ "$gh_out" = "$bb_out" ] || { echo "drift on fixture='${fixture}': gh='${gh_out}' bb='${bb_out}'" >&2; return 1; }
  done
}

@test "parity: truncate_body short inputs pass through unchanged in both scripts" {
  # This test only covers inputs well under both caps (GitHub=64000, Bitbucket=32000).
  # The two scripts intentionally produce DIFFERENT outputs for inputs between
  # 32001–64000 bytes (Bitbucket truncates; GitHub returns as-is) — so those
  # inputs are NOT compared here. See the per-script truncate_body tests above
  # for boundary coverage of each cap individually.
  local fixtures=(
    "hello world"
    ""
  )
  local gh_fn bb_fn
  # Extract truncate_body together with its MAX_BODY_SIZE constant from each
  # script so both run with their own cap (GitHub=64000, Bitbucket=32000).
  gh_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  for fixture in "${fixtures[@]}"; do
    local gh_out bb_out
    gh_out=$(bash -c "${gh_fn}"'; truncate_body "$1"' _ "$fixture")
    bb_out=$(bash -c "${bb_fn}"'; truncate_body "$1"' _ "$fixture")
    [ "$gh_out" = "$bb_out" ] || { echo "drift on fixture (len=${#fixture}): gh_len=${#gh_out} bb_len=${#bb_out}" >&2; return 1; }
  done
}

@test "parity: truncate_body mid-range input (33000 bytes): Bitbucket truncates, GitHub passes through" {
  # Inputs between 32001–64000 bytes must produce DIFFERENT results: Bitbucket
  # truncates (32000 cap), GitHub returns unchanged (64000 cap). This test
  # asserts the structural difference explicitly rather than checking equality.
  local mid_input
  mid_input=$(printf 'x%.0s' {1..33000})

  local gh_fn bb_fn
  gh_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")

  local gh_out bb_out
  gh_out=$(bash -c "${gh_fn}"'; truncate_body "$1"' _ "$mid_input")
  bb_out=$(bash -c "${bb_fn}"'; truncate_body "$1"' _ "$mid_input")

  # GitHub: 33000-byte input is under its 64000 cap — returned unchanged.
  [ "${#gh_out}" -eq 33000 ] || { echo "GitHub should return unchanged: got ${#gh_out} bytes" >&2; return 1; }
  [[ "$gh_out" != *"truncated"* ]] || { echo "GitHub should not truncate a 33000-byte body" >&2; return 1; }

  # Bitbucket: 33000-byte input exceeds its 32000 cap — truncated with notice.
  [[ "$bb_out" == *"Review output truncated"* ]] || { echo "Bitbucket should truncate a 33000-byte body" >&2; return 1; }
  local bb_bytes
  bb_bytes=$(printf '%s' "$bb_out" | wc -c)
  [ "$bb_bytes" -lt 32500 ] || { echo "Bitbucket truncated output too large: ${bb_bytes} bytes" >&2; return 1; }
}

@test "parity: mktemp_tracked registers files for cleanup in both scripts" {
  local gh_fn bb_fn
  gh_fn=$(awk '/^mktemp_tracked\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^mktemp_tracked\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")

  # Verify the core contract: the created file is registered in TMPFILES so
  # the EXIT trap can clean it up. A broken implementation that omits
  # TMPFILES+=("$f") would still echo the path and pass a file-exists check.
  #
  # mktemp_tracked must be called directly (not in $()) so TMPFILES+=("$f")
  # mutates the same shell's TMPFILES array — a $() subshell would get a copy
  # and the parent would never see the registration.
  local gh_result bb_result
  gh_result=$(bash -c '
    TMPFILES=()
    '"${gh_fn}"'
    mktemp_tracked /tmp/parity-gh-XXXXXXXX > /tmp/parity-gh-path.$$
    f=$(cat /tmp/parity-gh-path.$$); rm -f /tmp/parity-gh-path.$$
    [[ "${TMPFILES[0]}" == "$f" ]] && echo "ok:$f" || echo "not-registered:$f"
  ')
  bb_result=$(bash -c '
    TMPFILES=()
    '"${bb_fn}"'
    mktemp_tracked /tmp/parity-bb-XXXXXXXX > /tmp/parity-bb-path.$$
    f=$(cat /tmp/parity-bb-path.$$); rm -f /tmp/parity-bb-path.$$
    [[ "${TMPFILES[0]}" == "$f" ]] && echo "ok:$f" || echo "not-registered:$f"
  ')

  [[ "$gh_result" == ok:* ]] || { echo "GitHub mktemp_tracked did not register in TMPFILES: ${gh_result}" >&2; return 1; }
  [[ "$bb_result" == ok:* ]] || { echo "Bitbucket mktemp_tracked did not register in TMPFILES: ${bb_result}" >&2; return 1; }

  # Clean up the temp files (EXIT trap fires in the subshell, but the paths
  # are returned to us so we can double-check removal).
  local gh_path bb_path
  gh_path="${gh_result#ok:}"
  bb_path="${bb_result#ok:}"
  rm -f "$gh_path" "$bb_path"
}
