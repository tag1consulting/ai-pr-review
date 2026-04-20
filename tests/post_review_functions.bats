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
# _cleanup_duplicate_summary_comments — duplicate comment deletion
# ---------------------------------------------------------------------------
# Tests stub `gh` and `gh_api_retry` as shell functions (eval'd into the test
# shell via load_function), so no PATH shims are needed. Because `run` creates
# a subshell, assertions use temp files written by the stubs.

@test "_cleanup_duplicate_summary_comments: deletes IDs not matching kept_id" {
  load_function "${PROJECT_ROOT}/post-review.sh" _cleanup_duplicate_summary_comments

  OWNER="owner"; REPO="repo"; PR_NUMBER="1"
  MARKER_PREFIX="<!-- ai-pr-review-summary"
  DELETED_LOG=$(mktemp)

  # Stub: listing returns 3 IDs; DELETE records which id was targeted.
  # Called directly (no `run`) so function stubs are visible in the same shell.
  gh() {
    if printf '%s\n' "$@" | grep -q -- '--paginate'; then
      printf '100\n200\n300\n'
    elif [[ " $* " == *" --method DELETE "* ]] || [[ " $* " == *"--method DELETE"* ]]; then
      local id
      id=$(printf '%s\n' "$@" | grep -oE '/comments/[0-9]+' | grep -oE '[0-9]+$' | head -1)
      echo "$id" >> "$DELETED_LOG"
    fi
  }
  gh_api_retry() { gh "$@"; }

  _cleanup_duplicate_summary_comments "100"

  deleted=$(sort "$DELETED_LOG")
  [ "$deleted" = $'200\n300' ]
  rm -f "$DELETED_LOG"
}

@test "_cleanup_duplicate_summary_comments: does not delete the kept_id" {
  load_function "${PROJECT_ROOT}/post-review.sh" _cleanup_duplicate_summary_comments

  OWNER="owner"; REPO="repo"; PR_NUMBER="2"
  MARKER_PREFIX="<!-- ai-pr-review-summary"
  DELETED_LOG=$(mktemp)

  gh() {
    if printf '%s\n' "$@" | grep -q -- '--paginate'; then
      printf '42\n99\n'
    elif [[ " $* " == *" --method DELETE "* ]] || [[ " $* " == *"--method DELETE"* ]]; then
      local id
      id=$(printf '%s\n' "$@" | grep -oE '/comments/[0-9]+' | grep -oE '[0-9]+$' | head -1)
      echo "$id" >> "$DELETED_LOG"
    fi
  }
  gh_api_retry() { gh "$@"; }

  _cleanup_duplicate_summary_comments "99"

  deleted=$(cat "$DELETED_LOG")
  [ "$deleted" = "42" ]
  rm -f "$DELETED_LOG"
}

@test "_cleanup_duplicate_summary_comments: no-op when only kept_id exists" {
  load_function "${PROJECT_ROOT}/post-review.sh" _cleanup_duplicate_summary_comments

  OWNER="owner"; REPO="repo"; PR_NUMBER="3"
  MARKER_PREFIX="<!-- ai-pr-review-summary"
  DELETED_LOG=$(mktemp)

  gh() {
    if printf '%s\n' "$@" | grep -q -- '--paginate'; then
      printf '42\n'
    elif [[ " $* " == *" --method DELETE "* ]] || [[ " $* " == *"--method DELETE"* ]]; then
      local id
      id=$(printf '%s\n' "$@" | grep -oE '/comments/[0-9]+' | grep -oE '[0-9]+$' | head -1)
      echo "$id" >> "$DELETED_LOG"
    fi
  }
  gh_api_retry() { gh "$@"; }

  _cleanup_duplicate_summary_comments "42"

  deleted=$(cat "$DELETED_LOG")
  [ -z "$deleted" ]
  rm -f "$DELETED_LOG"
}

@test "_cleanup_duplicate_summary_comments: continues and returns 0 when a DELETE fails" {
  load_function "${PROJECT_ROOT}/post-review.sh" _cleanup_duplicate_summary_comments

  OWNER="owner"; REPO="repo"; PR_NUMBER="4"
  MARKER_PREFIX="<!-- ai-pr-review-summary"
  DELETED_LOG=$(mktemp)

  # 200 fails, 300 succeeds; function must still return 0 and process 300
  gh() {
    if printf '%s\n' "$@" | grep -q -- '--paginate'; then
      printf '100\n200\n300\n'
    elif [[ " $* " == *" --method DELETE "* ]] || [[ " $* " == *"--method DELETE"* ]]; then
      local id
      id=$(printf '%s\n' "$@" | grep -oE '/comments/[0-9]+' | grep -oE '[0-9]+$' | head -1)
      if [ "$id" = "200" ]; then return 1; fi
      echo "$id" >> "$DELETED_LOG"
    fi
  }
  gh_api_retry() { gh "$@"; }

  # Must return 0 even when a DELETE fails
  _cleanup_duplicate_summary_comments "100"
  local retval=$?
  [ "$retval" -eq 0 ]

  deleted=$(cat "$DELETED_LOG")
  [ "$deleted" = "300" ]
  rm -f "$DELETED_LOG"
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
  # Prefix one ASCII byte so 64000-byte cut lands mid-codepoint (not on a
  # 3-byte boundary). Without the prefix, 64000 is divisible by 3 and the
  # cut would align on a codepoint, masking regressions where iconv is
  # removed. With the prefix, iconv must drop a partial codepoint.
  local body
  body="x$(printf '日%.0s' $(seq 1 30000))"
  run truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  # Round-trip through iconv: valid UTF-8 passes through byte-identical.
  local original_len roundtrip_len
  original_len=$(printf '%s' "$output" | wc -c)
  roundtrip_len=$(printf '%s' "$output" | iconv -f UTF-8 -t UTF-8 2>/dev/null | wc -c)
  [ "$original_len" -eq "$roundtrip_len" ]
  # Total body (truncated content + notice) must stay under GitHub's
  # 65,536-byte hard limit.
  [ "$original_len" -lt 65536 ]
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

# ---------------------------------------------------------------------------
# _truncate_body — standalone-mode sibling (post_standalone_issue path)
# ---------------------------------------------------------------------------
# _truncate_body has identical byte-count logic but a shorter notice and a
# hardcoded limit. Tests duplicated to catch drift between the two copies.

setup_standalone_truncate_body() {
  load_function "${PROJECT_ROOT}/post-review.sh" _truncate_body
}

@test "_truncate_body: short ASCII body returned unchanged" {
  setup_standalone_truncate_body
  run _truncate_body "hello world"
  [ "$status" -eq 0 ]
  [ "$output" = "hello world" ]
}

@test "_truncate_body: ASCII body over limit is truncated with notice" {
  setup_standalone_truncate_body
  local body
  body=$(printf 'a%.0s' $(seq 1 70000))
  run _truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  local byte_len
  byte_len=$(printf '%s' "$output" | wc -c)
  [ "$byte_len" -lt 65536 ]
}

@test "_truncate_body: multi-byte UTF-8 truncation produces valid UTF-8" {
  setup_standalone_truncate_body
  local body
  body="x$(printf '日%.0s' $(seq 1 30000))"
  run _truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  local original_len roundtrip_len
  original_len=$(printf '%s' "$output" | wc -c)
  roundtrip_len=$(printf '%s' "$output" | iconv -f UTF-8 -t UTF-8 2>/dev/null | wc -c)
  [ "$original_len" -eq "$roundtrip_len" ]
  [ "$original_len" -lt 65536 ]
}

@test "_truncate_body: 50k multi-byte chars (150kB) triggers truncation" {
  setup_standalone_truncate_body
  local body
  body=$(printf '日%.0s' $(seq 1 50000))
  run _truncate_body "$body"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
}
