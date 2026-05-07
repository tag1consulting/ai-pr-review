#!/usr/bin/env bats
# Tests for run-semgrep.sh. Uses SEMGREP_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-semgrep.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/semgrep"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "semgrep: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "semgrep: nonexistent file path returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/file.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "semgrep: empty semgrep output returns empty array" {
  touch "$WORK/app.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-empty.json" run --separate-stderr "$SCRIPT" "$WORK/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "semgrep: malformed output falls through safely" {
  touch "$WORK/app.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/app.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "semgrep: ERROR severity maps to High" {
  touch "$WORK/runner.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-error.json" run --separate-stderr "$SCRIPT" "$WORK/runner.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
}

@test "semgrep: WARNING severity maps to Medium" {
  touch "$WORK/utils.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-warning.json" run --separate-stderr "$SCRIPT" "$WORK/utils.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "semgrep: findings conform to required schema" {
  touch "$WORK/runner.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-error.json" run --separate-stderr "$SCRIPT" "$WORK/runner.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "semgrep: confidence is 90 on all findings" {
  touch "$WORK/runner.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-error.json" run --separate-stderr "$SCRIPT" "$WORK/runner.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 90)' > /dev/null
}

@test "semgrep: source field is 'semgrep' on all findings" {
  touch "$WORK/runner.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-error.json" run --separate-stderr "$SCRIPT" "$WORK/runner.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "semgrep")' > /dev/null
}

@test "semgrep: finding text contains check_id" {
  touch "$WORK/runner.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-error.json" run --separate-stderr "$SCRIPT" "$WORK/runner.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("subprocess-shell-true")' > /dev/null
}

@test "semgrep: remediation includes reference URL when present" {
  touch "$WORK/runner.py"
  SEMGREP_MOCK_FILE="$FIXTURES/semgrep-error.json" run --separate-stderr "$SCRIPT" "$WORK/runner.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("semgrep.dev")' > /dev/null
}

# ---------------------------------------------------------------------------
# Baked-in ruleset resolution
# ---------------------------------------------------------------------------
#
# These tests stub `semgrep` on PATH with a shell wrapper that writes its
# argv to $SEMGREP_ARGV_LOG (readable by the test) and prints a valid
# empty-results JSON on stdout. We can't use stderr because run-semgrep.sh
# redirects the real binary's stderr to a temp file.

_install_semgrep_stub() {
  STUB_DIR=$(mktemp -d)
  SEMGREP_ARGV_LOG=$(mktemp)
  cat > "$STUB_DIR/semgrep" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$SEMGREP_ARGV_LOG"
echo '{"results": []}'
EOF
  chmod +x "$STUB_DIR/semgrep"
  export SEMGREP_ARGV_LOG
}

_cleanup_semgrep_stub() {
  rm -rf "$STUB_DIR"
  rm -f "$SEMGREP_ARGV_LOG"
}

@test "semgrep: uses baked-in rules when SEMGREP_RULES_DIR contains .yml files" {
  touch "$WORK/app.py"
  RULES_DIR=$(mktemp -d)
  cat > "$RULES_DIR/ci.yml" <<'YAML'
rules:
  - id: stub.rule
    pattern: x
    message: stub
    languages: [python]
    severity: WARNING
YAML

  _install_semgrep_stub

  PATH="$STUB_DIR:$PATH" SEMGREP_RULES_DIR="$RULES_DIR" \
    run --separate-stderr "$SCRIPT" "$WORK/app.py"

  argv=$(cat "$SEMGREP_ARGV_LOG")

  rm -rf "$RULES_DIR"
  _cleanup_semgrep_stub

  [ "$status" -eq 0 ]
  # argv should show the baked-in rule file, not --config=auto
  [[ "$argv" == *"--config ${RULES_DIR%/}/ci.yml"* ]] \
    || { echo "argv was: $argv"; false; }
  [[ "$argv" != *"--config=auto"* ]] \
    || { echo "argv was: $argv"; false; }
}

@test "semgrep: falls back to --config=auto when SEMGREP_RULES_DIR is absent" {
  touch "$WORK/app.py"
  _install_semgrep_stub

  PATH="$STUB_DIR:$PATH" SEMGREP_RULES_DIR="/nonexistent/path/$$" \
    run --separate-stderr "$SCRIPT" "$WORK/app.py"

  argv=$(cat "$SEMGREP_ARGV_LOG")
  _cleanup_semgrep_stub

  [ "$status" -eq 0 ]
  [[ "$argv" == *"--config=auto"* ]] \
    || { echo "argv was: $argv"; false; }
}

@test "semgrep: falls back to --config=auto when SEMGREP_RULES_DIR has no .yml files" {
  touch "$WORK/app.py"
  RULES_DIR=$(mktemp -d)  # empty dir
  _install_semgrep_stub

  PATH="$STUB_DIR:$PATH" SEMGREP_RULES_DIR="$RULES_DIR" \
    run --separate-stderr "$SCRIPT" "$WORK/app.py"

  argv=$(cat "$SEMGREP_ARGV_LOG")

  rm -rf "$RULES_DIR"
  _cleanup_semgrep_stub

  [ "$status" -eq 0 ]
  [[ "$argv" == *"--config=auto"* ]] \
    || { echo "argv was: $argv"; false; }
}
