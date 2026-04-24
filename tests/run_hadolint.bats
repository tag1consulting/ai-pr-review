#!/usr/bin/env bats
# Tests for run-hadolint.sh. Uses HADOLINT_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/run-hadolint.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/hadolint"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "hadolint: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "hadolint: non-Dockerfile file path returns empty array" {
  touch "$WORK/app.py"
  run --separate-stderr "$SCRIPT" "$WORK/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "hadolint: nonexistent Dockerfile returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/Dockerfile"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "hadolint: empty output returns empty array" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-empty.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "hadolint: malformed output falls through safely" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# File extension matching
# ---------------------------------------------------------------------------

@test "hadolint: bare Dockerfile name triggers scan" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "hadolint: Dockerfile.production name triggers scan" {
  touch "$WORK/Dockerfile.production"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile.production"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "hadolint: .dockerfile extension triggers scan" {
  touch "$WORK/web.dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/web.dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "hadolint: error level maps to High" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "hadolint: warning level maps to Medium" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-warning.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

@test "hadolint: info level maps to Low" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-info.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Low"' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "hadolint: findings conform to required schema" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "hadolint: source field is 'hadolint' on all findings" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "hadolint")' > /dev/null
}

@test "hadolint: confidence is 90 on all findings" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 90)' > /dev/null
}

@test "hadolint: finding text contains check code" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("DL3008")' > /dev/null
}

@test "hadolint: remediation includes wiki URL" {
  touch "$WORK/Dockerfile"
  HADOLINT_MOCK_FILE="$FIXTURES/hadolint-error.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("hadolint")' > /dev/null
}
