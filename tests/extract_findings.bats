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
  run extract_findings "$fixture"
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
