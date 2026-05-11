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
  source "${PROJECT_ROOT}/lib/findings.sh"
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

# ---------------------------------------------------------------------------
# source field stamping
# ---------------------------------------------------------------------------

@test "extract_findings: stamps agent name as source when source absent" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings.md"
  run extract_findings "$fixture" "code-reviewer"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "code-reviewer")' > /dev/null
}

@test "extract_findings: preserves explicit source field from agent output" {
  local fixture
  fixture=$(mktemp /tmp/bats-findings-XXXXXXXX.md)
  cat > "$fixture" <<'EOF'
```json-findings
[{"severity":"High","confidence":90,"file":"a.go","line":1,"finding":"test","remediation":"fix","source":"shellcheck"}]
```
EOF
  run extract_findings "$fixture" "code-reviewer"
  [ "$status" -eq 0 ]
  # source from the finding wins over the agent_name arg
  echo "$output" | jq -e '.[0].source == "shellcheck"' > /dev/null
  rm -f "$fixture"
}

@test "extract_findings: defaults source to unknown when no agent name given" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings.md"
  run extract_findings "$fixture"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "unknown")' > /dev/null
}

@test "extract_findings: salvaged truncated findings also get source stamped" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/truncated-findings.md"
  touch "${fixture}.truncated"
  run --separate-stderr extract_findings "$fixture" "security-reviewer"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "security-reviewer")' > /dev/null
}

# ---------------------------------------------------------------------------
# suggestion fields (suggested_code, start_line)
# ---------------------------------------------------------------------------

@test "extract_findings: preserves suggested_code field" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings-with-suggestions.md"
  run extract_findings "$fixture"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].suggested_code | test("os.Open")' > /dev/null
  echo "$output" | jq -e '.[1].suggested_code | test("defer f.Close")' > /dev/null
}

@test "extract_findings: preserves start_line field when present" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings-with-suggestions.md"
  run extract_findings "$fixture"
  [ "$status" -eq 0 ]
  # Second finding has start_line=15; first finding does not have start_line
  echo "$output" | jq -e '.[1].start_line == 15' > /dev/null
  echo "$output" | jq -e '.[0] | has("start_line") | not' > /dev/null
}

@test "extract_findings: suggestion fields coexist with source stamping" {
  local fixture="${PROJECT_ROOT}/tests/fixtures/sample-findings-with-suggestions.md"
  run extract_findings "$fixture" "code-reviewer"
  [ "$status" -eq 0 ]
  # All fields (old and new) must survive the source stamping merge
  echo "$output" | jq -e 'all(.[]; .source == "code-reviewer" and has("suggested_code"))' > /dev/null
}
