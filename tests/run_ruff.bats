#!/usr/bin/env bats
# Tests for run-ruff.sh. Uses RUFF_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-ruff.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/ruff"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "ruff: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "ruff: no .py files in input returns empty array" {
  run --separate-stderr "$SCRIPT" $'src/main.go\nREADME.md'
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "ruff: nonexistent .py path returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "ruff: empty ruff output returns empty array" {
  cp "$FIXTURES/sample.py" "$WORK/sample.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-empty.json" run --separate-stderr "$SCRIPT" "$WORK/sample.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "ruff: F-prefix code maps to High" {
  cp "$FIXTURES/sample.py" "$WORK/views.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-error.json" run --separate-stderr "$SCRIPT" "$WORK/views.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "ruff: W-prefix code maps to Medium" {
  cp "$FIXTURES/sample.py" "$WORK/models.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-warning.json" run --separate-stderr "$SCRIPT" "$WORK/models.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "ruff: findings conform to required schema" {
  cp "$FIXTURES/sample.py" "$WORK/views.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-error.json" run --separate-stderr "$SCRIPT" "$WORK/views.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "ruff: confidence is 90 on all findings" {
  cp "$FIXTURES/sample.py" "$WORK/views.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-error.json" run --separate-stderr "$SCRIPT" "$WORK/views.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 90)' > /dev/null
}

@test "ruff: source field is 'ruff' on all findings" {
  cp "$FIXTURES/sample.py" "$WORK/views.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-error.json" run --separate-stderr "$SCRIPT" "$WORK/views.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "ruff")' > /dev/null
}

@test "ruff: finding text contains rule code" {
  cp "$FIXTURES/sample.py" "$WORK/views.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-error.json" run --separate-stderr "$SCRIPT" "$WORK/views.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("F401")' > /dev/null
}

@test "ruff: remediation includes URL" {
  cp "$FIXTURES/sample.py" "$WORK/views.py"
  RUFF_MOCK_FILE="$FIXTURES/ruff-error.json" run --separate-stderr "$SCRIPT" "$WORK/views.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("astral.sh|ruff")' > /dev/null
}
