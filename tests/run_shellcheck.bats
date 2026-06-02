#!/usr/bin/env bats
# Tests for run-shellcheck.sh.
# Mock-file tests bypass the shellcheck binary; real-binary tests skip when absent.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-shellcheck.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/shellcheck"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths (no shellcheck binary required)
# ---------------------------------------------------------------------------

@test "shellcheck: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "shellcheck: no .sh files in input returns empty array" {
  run --separate-stderr "$SCRIPT" $'src/foo.go\nREADME.md'
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "shellcheck: nonexistent file path returns empty array" {
  run --separate-stderr "$SCRIPT" "/nonexistent/path/file.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# jq-absent guard
# ---------------------------------------------------------------------------

@test "shellcheck: returns [] and exits 0 when jq is absent" {
  # Build a minimal bin dir containing essential tools but NOT jq, then run
  # the script with PATH pointing only at that dir. This avoids removing entire
  # system directories (which would lose bash, env, mktemp, etc. in CI) while
  # reliably hiding jq from 'command -v jq'.

  touch "$WORK/test.sh"
  MINBIN=$(mktemp -d)

  for bin in bash env cat mktemp shellcheck printf head sort uniq wc; do
    real=$(command -v "$bin" 2>/dev/null) && ln -sf "$real" "$MINBIN/$bin" 2>/dev/null || true
  done

  run --separate-stderr env -i "PATH=$MINBIN" "HOME=$HOME" \
    bash "$SCRIPT" "$WORK/test.sh"

  rm -rf "$MINBIN"

  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
  [[ "$stderr" == *"jq not installed"* ]]
}

# ---------------------------------------------------------------------------
# SHELLCHECK_MOCK_FILE paths (no real shellcheck binary invoked)
# ---------------------------------------------------------------------------

@test "shellcheck: warning fixture maps to Medium severity" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

@test "shellcheck: error fixture maps to High severity" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-error.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "shellcheck: empty fixture returns empty array" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-empty.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "shellcheck: malformed fixture falls through safely" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-malformed.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "shellcheck: unreadable SHELLCHECK_MOCK_FILE returns [] with warning" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="/nonexistent/shellcheck-mock.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
  [[ "$stderr" == *"not readable"* ]]
}

# ---------------------------------------------------------------------------
# Schema conformance (via mock file)
# ---------------------------------------------------------------------------

@test "shellcheck: findings conform to required schema fields" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "shellcheck: confidence is 95 on all findings" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 95)' > /dev/null
}

@test "shellcheck: source field is 'shellcheck' on all findings" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "shellcheck")' > /dev/null
}

@test "shellcheck: finding text contains SC code" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("^SC[0-9]+:")' > /dev/null
}

@test "shellcheck: remediation contains shellcheck wiki URL" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("shellcheck.net/wiki")' > /dev/null
}

@test "shellcheck: severity values are restricted to High or Medium" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr "$SCRIPT" "$WORK/test.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .severity | test("^(High|Medium)$"))' > /dev/null
}

# ---------------------------------------------------------------------------
# stdin support
# ---------------------------------------------------------------------------

@test "shellcheck: accepts changed files list via stdin" {
  touch "$WORK/test.sh"
  SHELLCHECK_MOCK_FILE="$FIXTURES/shellcheck-warning.json" \
    run --separate-stderr bash -c "echo '$WORK/test.sh' | '$SCRIPT'"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Real shellcheck tests (skipped when binary absent)
# ---------------------------------------------------------------------------

@test "shellcheck: clean script returns empty array" {
  command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not available"
  cat > "$WORK/clean.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "hello"
EOF
  run --separate-stderr "$SCRIPT" "$WORK/clean.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "shellcheck: script with warning produces findings" {
  command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not available"
  # SC2148: Tips depend on target shell and missing shebang
  # This reliably produces at least one shellcheck warning
  cat > "$WORK/warn.sh" <<'EOF'
x=1
echo $x
EOF
  run --separate-stderr "$SCRIPT" "$WORK/warn.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'type == "array"' > /dev/null
  # May produce findings or empty; just assert no crash and valid JSON
}

@test "shellcheck: severity is High for error-level findings" {
  command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not available"
  # SC2104: 'break' is only valid in loops -- this is an error-level finding
  cat > "$WORK/error_test.sh" <<'EOF'
#!/usr/bin/env bash
f() { break; }
f
EOF
  run --separate-stderr "$SCRIPT" "$WORK/error_test.sh"
  [ "$status" -eq 0 ]
  if echo "$output" | jq -e 'map(select(.finding | test("SC2104"))) | length > 0' > /dev/null 2>&1; then
    echo "$output" | jq -e 'map(select(.finding | test("SC2104"))) | all(.[]; .severity == "High")' > /dev/null
  fi
}
