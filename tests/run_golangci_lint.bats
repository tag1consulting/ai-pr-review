#!/usr/bin/env bats
# Tests for run-golangci-lint.sh. Uses GOLANGCI_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/run-golangci-lint.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/golangci"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "golangci: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "golangci: no .go files in input returns empty array" {
  run --separate-stderr "$SCRIPT" $'src/main.py\nREADME.md'
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "golangci: nonexistent .go path returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/main.go"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "golangci: empty golangci output returns empty array" {
  cp "$FIXTURES/sample.go" "$WORK/sample.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-empty.json" run --separate-stderr "$SCRIPT" "$WORK/sample.go"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "golangci: errcheck linter maps to High" {
  cp "$FIXTURES/sample.go" "$WORK/db.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-high.json" run --separate-stderr "$SCRIPT" "$WORK/db.go"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "golangci: gofmt linter maps to Medium" {
  cp "$FIXTURES/sample.go" "$WORK/helper.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-medium.json" run --separate-stderr "$SCRIPT" "$WORK/helper.go"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "golangci: findings conform to required schema" {
  cp "$FIXTURES/sample.go" "$WORK/db.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-high.json" run --separate-stderr "$SCRIPT" "$WORK/db.go"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "golangci: confidence is 90 on all findings" {
  cp "$FIXTURES/sample.go" "$WORK/db.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-high.json" run --separate-stderr "$SCRIPT" "$WORK/db.go"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 90)' > /dev/null
}

@test "golangci: source field is 'golangci-lint' on all findings" {
  cp "$FIXTURES/sample.go" "$WORK/db.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-high.json" run --separate-stderr "$SCRIPT" "$WORK/db.go"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "golangci-lint")' > /dev/null
}

@test "golangci: finding text contains linter name" {
  cp "$FIXTURES/sample.go" "$WORK/db.go"
  GOLANGCI_MOCK_FILE="$FIXTURES/golangci-high.json" run --separate-stderr "$SCRIPT" "$WORK/db.go"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("errcheck")' > /dev/null
}
