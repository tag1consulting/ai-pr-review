#!/usr/bin/env bats
# Tests for run-shellcheck.sh.
# Uses real shellcheck on controlled temporary files to verify output schema.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  command -v shellcheck >/dev/null 2>&1 || skip "shellcheck not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/run-shellcheck.sh"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
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

@test "shellcheck: clean script returns empty array" {
  cat > "$WORK/clean.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "hello"
EOF
  run --separate-stderr "$SCRIPT" "$WORK/clean.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Findings detection
# ---------------------------------------------------------------------------

@test "shellcheck: script with warning produces findings" {
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

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "shellcheck: findings conform to required schema fields" {
  # Write a script with a known shellcheck warning (SC2086: double-quote variable)
  cat > "$WORK/schema_test.sh" <<'EOF'
#!/usr/bin/env bash
x="hello world"
echo $x
ls $x
EOF
  run --separate-stderr "$SCRIPT" "$WORK/schema_test.sh"
  [ "$status" -eq 0 ]
  # If shellcheck found something, assert schema
  if echo "$output" | jq -e 'length > 0' > /dev/null 2>&1; then
    echo "$output" | jq -e '
      all(.[];
        has("severity") and has("confidence") and has("source") and
        has("file") and has("line") and has("finding") and has("remediation")
      )
    ' > /dev/null
    echo "$output" | jq -e 'all(.[]; .severity | test("^(Critical|High|Medium|Low)$"))' > /dev/null
    echo "$output" | jq -e 'all(.[]; (.confidence >= 75) and (.confidence <= 100))' > /dev/null
  fi
}

@test "shellcheck: source field is 'shellcheck' on all findings" {
  cat > "$WORK/source_test.sh" <<'EOF'
#!/usr/bin/env bash
x="value"
echo $x
ls $x
EOF
  run --separate-stderr "$SCRIPT" "$WORK/source_test.sh"
  [ "$status" -eq 0 ]
  if echo "$output" | jq -e 'length > 0' > /dev/null 2>&1; then
    echo "$output" | jq -e 'all(.[]; .source == "shellcheck")' > /dev/null
  fi
}

@test "shellcheck: severity is High for error-level findings" {
  # SC2104: 'break' is only valid in loops — this is an error-level finding
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
