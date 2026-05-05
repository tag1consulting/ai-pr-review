#!/usr/bin/env bats
# Tests for run-phpcs.sh. Uses PHPCS_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-phpcs.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/phpcs"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "phpcs: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpcs: non-PHP file returns empty array" {
  touch "$WORK/app.py"
  run --separate-stderr "$SCRIPT" "$WORK/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpcs: nonexistent PHP file returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/file.php"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpcs: empty results returns empty array" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-empty.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpcs: malformed output falls through safely" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# File extension matching
# ---------------------------------------------------------------------------

@test "phpcs: .module extension triggers scan" {
  touch "$WORK/my_module.module"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/my_module.module"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "phpcs: .install extension triggers scan" {
  touch "$WORK/my_module.install"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/my_module.install"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "phpcs: .theme extension triggers scan" {
  touch "$WORK/my_theme.theme"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/my_theme.theme"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "phpcs: ERROR type maps to High" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "phpcs: WARNING type maps to Medium" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-warning.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "phpcs: findings conform to required schema" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "phpcs: source field is 'phpcs' on all findings" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "phpcs")' > /dev/null
}

@test "phpcs: confidence is 90 on all findings" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 90)' > /dev/null
}

@test "phpcs: finding text contains sniff source" {
  touch "$WORK/module.php"
  PHPCS_MOCK_FILE="$FIXTURES/phpcs-error.json" run --separate-stderr "$SCRIPT" "$WORK/module.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("Generic.PHP.UpperCaseConstant")' > /dev/null
}
