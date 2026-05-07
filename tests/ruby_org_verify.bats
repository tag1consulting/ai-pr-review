#!/usr/bin/env bats
# Tests for the ruby-org verify type added in review.sh's apply_suppressions().
#
# The verify case block is embedded inside apply_suppressions() and reached via
# a nested while-loop that processes suppressed findings with a `verify` field.
# Rather than replicate that harness, these tests extract the ruby-org case
# body and evaluate it with a stubbed curl so we can assert on:
#   1. The version regex extraction from finding text
#   2. The canonical URL that gets queried
#   3. The suppression/restoration decision based on curl exit status
#
# This mirrors the existing load_function pattern in test_helper.bash — we
# pluck out a bounded chunk of script, stub its dependencies, and run it.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  WORK=$(mktemp -d)
  # Log file where our stubbed curl records the URL it would have fetched.
  CURL_LOG="${WORK}/curl.log"
  # Marker file written by the stubbed _suppression_restore so we can detect it.
  RESTORE_MARKER="${WORK}/restored"

  # Extract the ruby-org case body from review.sh — the lines BETWEEN the
  # "ruby-org)" header and the closing ";;". We strip both so the body can be
  # wrapped in a fresh function for testing.
  RUBY_ORG_BODY=$(awk '
    /^      ruby-org\)/ { inblock=1; next }
    inblock && /^        ;;/ { exit }
    inblock { print }
  ' "${PROJECT_ROOT}/review.sh")

  [[ -n "$RUBY_ORG_BODY" ]] || { echo "failed to extract ruby-org block"; return 1; }
}

teardown() {
  rm -rf "$WORK"
}

# Evaluate the ruby-org case body with finding_text pre-set and curl stubbed.
# $1 — finding_text
# $2 — curl exit code (0 = version exists, 22 = 404, other = error)
_run_case() {
  local finding_text="$1"
  local curl_exit="$2"
  # Stub curl: log the URL (last arg) and exit with the requested code.
  # Stub _suppression_restore: write a marker file with the JSON it received.
  # shellcheck disable=SC2030,SC2031
  (
    curl() {
      # Log every URL we see; only the ruby-lang.org URL matters for assertions
      for arg in "$@"; do
        case "$arg" in
          https://*) echo "$arg" >> "$CURL_LOG" ;;
        esac
      done
      return "$curl_exit"
    }
    export -f curl
    _suppression_restore() { echo "$1" > "$RESTORE_MARKER"; }
    export -f _suppression_restore
    # shellcheck disable=SC2034
    finding_text="$finding_text"
    # shellcheck disable=SC2034
    finding_json='{"finding":"'"$finding_text"'"}'

    # The extracted block is the contents of a `case` arm. Evaluate it in a
    # function so the `continue` statement has a loop to exit (the original
    # location is inside a `while` loop reading suppressed findings).
    eval "_ruby_org_arm() { for _ in 1; do ${RUBY_ORG_BODY}; done; }"
    _ruby_org_arm
  ) 2>&1
}

@test "ruby-org: extracts X.Y.Z and queries cache.ruby-lang.org" {
  _run_case "Ruby version '4.0.3' is not a valid/released Ruby version" 0
  [ -f "$CURL_LOG" ]
  grep -q "cache.ruby-lang.org/pub/ruby/4.0/ruby-4.0.3.tar.gz" "$CURL_LOG"
}

@test "ruby-org: suppression stands when curl returns 0 (version exists)" {
  _run_case "Ruby 4.0.3 is unreleased" 0
  # _suppression_restore must NOT have been called
  [ ! -f "$RESTORE_MARKER" ]
}

@test "ruby-org: finding restored when curl returns non-zero (version missing)" {
  _run_case "Ruby 9.9.9 does not exist" 22
  # _suppression_restore MUST have been called with the finding JSON
  [ -f "$RESTORE_MARKER" ]
  grep -q "Ruby 9.9.9" "$RESTORE_MARKER"
}

@test "ruby-org: handles compound minor versions (3.14.2)" {
  _run_case "Ruby 3.14.2 appears invalid" 0
  grep -q "cache.ruby-lang.org/pub/ruby/3.14/ruby-3.14.2.tar.gz" "$CURL_LOG"
}

@test "ruby-org: bails out when no X.Y.Z pattern in finding text" {
  _run_case "Ruby is not valid" 0
  # No curl log should be written — the `continue` should have fired before curl
  [ ! -f "$CURL_LOG" ]
  [ ! -f "$RESTORE_MARKER" ]
}

@test "ruby-org: picks first version when multiple appear" {
  _run_case "Ruby 4.0.3 and 3.4.1 both flagged" 0
  # Should have queried 4.0.3, the first version in the text
  grep -q "ruby-4.0.3.tar.gz" "$CURL_LOG"
  ! grep -q "ruby-3.4.1.tar.gz" "$CURL_LOG"
}
