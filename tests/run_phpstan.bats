#!/usr/bin/env bats
# Tests for run-phpstan.sh. Uses PHPSTAN_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-phpstan.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/phpstan"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "phpstan: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpstan: non-PHP file returns empty array" {
  touch "$WORK/app.py"
  run --separate-stderr "$SCRIPT" "$WORK/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpstan: nonexistent PHP file returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/file.php"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpstan: empty results returns empty array" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-empty.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "phpstan: malformed output falls through safely" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# File extension matching
# ---------------------------------------------------------------------------

@test "phpstan: .php extension triggers scan" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "phpstan: .module extension triggers scan" {
  touch "$WORK/my_module.module"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/my_module.module"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "phpstan: .install extension triggers scan" {
  touch "$WORK/my_module.install"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/my_module.install"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "phpstan: all findings map to High severity" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e 'all(.[]; .severity == "High")' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "phpstan: findings conform to required schema" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "phpstan: source field is 'phpstan' on all findings" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "phpstan")' > /dev/null
}

@test "phpstan: confidence is 85 on all findings" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 85)' > /dev/null
}

@test "phpstan: finding text contains the phpstan message" {
  touch "$WORK/MyService.php"
  PHPSTAN_MOCK_FILE="$FIXTURES/phpstan-error.json" run --separate-stderr "$SCRIPT" "$WORK/MyService.php"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("NodeInterface")' > /dev/null
}
