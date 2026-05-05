#!/usr/bin/env bats
# Tests for run-tflint.sh. Uses TFLINT_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-tflint.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/tflint"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "tflint: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "tflint: non-Terraform file returns empty array" {
  touch "$WORK/app.py"
  run --separate-stderr "$SCRIPT" "$WORK/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "tflint: nonexistent .tf file returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/main.tf"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "tflint: empty issues returns empty array" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-empty.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "tflint: malformed output falls through safely" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# File extension matching
# ---------------------------------------------------------------------------

@test "tflint: .tf extension triggers scan" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "tflint: .tfvars extension triggers scan" {
  touch "$WORK/terraform.tfvars"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/terraform.tfvars"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "tflint: error severity maps to High" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "tflint: warning severity maps to Medium" {
  touch "$WORK/variables.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-warning.json" run --separate-stderr "$SCRIPT" "$WORK/variables.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "tflint: findings conform to required schema" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "tflint: source field is 'tflint' on all findings" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "tflint")' > /dev/null
}

@test "tflint: confidence is 90 on all findings" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 90)' > /dev/null
}

@test "tflint: finding text contains rule name" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("aws_instance_invalid_type")' > /dev/null
}

@test "tflint: remediation includes link when present" {
  touch "$WORK/main.tf"
  TFLINT_MOCK_FILE="$FIXTURES/tflint-error.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("tflint")' > /dev/null
}
