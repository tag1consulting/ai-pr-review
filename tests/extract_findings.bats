#!/usr/bin/env bats
# Tests for extract_findings() in review.sh.
# Uses fixture files to test the JSON extraction and validation logic.
#
# Note: run --separate-stderr is used for error-path tests because extract_findings
# writes a WARNING to stderr on invalid input. Without it, bats merges stdout+stderr
# into $output and the comparison to "[]" would fail.
bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  load_function "${PROJECT_ROOT}/review.sh" extract_findings
}

teardown() {
  # Remove any sidecar files left by truncation tests
  rm -f "${PROJECT_ROOT}/tests/fixtures/"*.truncated
}

# ---------------------------------------------------------------------------
# extract_findings
# ---------------------------------------------------------------------------

@test "extract_findings: valid block returns JSON array" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings.md"
  run extract_findings "$fixture"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'type == "array"' > /dev/null
}

@test "extract_findings: valid block returns expected finding count" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings.md"
  run extract_findings "$fixture"
  [ "$status" -eq 0 ]
  local count
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 2 ]
}

@test "extract_findings: valid block preserves severity field" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings.md"
  run extract_findings "$fixture"
  [ "$status" -eq 0 ]
  local first_severity
  first_severity=$(echo "$output" | jq -r '.[0].severity')
  [ "$first_severity" = "High" ]
}

@test "extract_findings: no findings block returns empty array" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/no-findings.md"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "extract_findings: malformed JSON returns empty array" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/invalid-findings.md"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "extract_findings: findings missing required fields returns empty array" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/missing-fields-findings.md"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "extract_findings: no block emits WARNING" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/no-findings.md"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
  [[ "$stderr" == *"WARNING"* ]]
}

@test "extract_findings: truncated response with no fence returns empty array with truncation warning" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/truncated-no-fence.md"
  touch "${fixture}.truncated"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
  [[ "$stderr" == *"truncated"* ]]
}

@test "extract_findings: truncated JSON block is salvaged and returns partial findings" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/truncated-findings.md"
  touch "${fixture}.truncated"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  local count
  count=$(echo "$output" | jq 'length')
  [ "$count" -ge 2 ]
  [[ "$stderr" == *"salvaged"* ]]
}

@test "extract_findings: salvaged findings pass schema validation" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/truncated-findings.md"
  touch "${fixture}.truncated"
  run --separate-stderr extract_findings "$fixture"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    type == "array" and
    all(.[]; has("severity") and has("finding") and has("confidence"))
  ' > /dev/null
}
