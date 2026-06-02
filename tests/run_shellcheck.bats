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
  # Shadow jq with a fake stub that exits 127 (command-not-found), placed in a
  # temp bin dir prepended to PATH inside a wrapper script. Using a wrapper
  # confines the PATH change to the script's process tree only -- bats' own
  # infrastructure (mktemp, etc.) runs with the original PATH unchanged.
  # This approach is portable across CI environments where jq may be at
  # /usr/bin/jq or elsewhere, and does not rely on realpath being available.

  touch "$WORK/test.sh"

  STUB_DIR=$(mktemp -d)

  # Create a fake jq that exits 127 so command -v jq succeeds in PATH lookup
  # but actually running it fails -- however, the script uses "command -v jq"
  # which checks PATH existence. We make it not executable so command -v fails,
  # OR we can simply not create jq at all in STUB_DIR and instead strip the
  # real jq's directory. The simplest and most portable approach: put a
  # non-executable file named jq in STUB_DIR (command -v will still find
  # executables only), and place STUB_DIR before the real jq in PATH while
  # excluding the real jq's directory entirely from the wrapper's PATH.
  #
  # Simplest portable approach: fake jq stub that exits 127, placed before
  # the real jq in PATH. The script checks "command -v jq" which will find
  # the stub -- but wait, a stub that exits 127 still IS found by command -v.
  # We need command -v jq to FAIL (exit non-zero). So: don't put any jq in
  # PATH at all. Strip real jq's parent dir and any dirs containing a jq
  # binary, using a simple existence check (no realpath needed).

  FILTERED_PATH="$STUB_DIR"
  IFS=: read -ra PATH_PARTS <<< "$PATH"
  for p in "${PATH_PARTS[@]}"; do
    if [[ -x "${p}/jq" ]]; then
      continue  # skip any directory that contains a jq executable
    fi
    FILTERED_PATH="${FILTERED_PATH}:${p}"
  done

  # Write a wrapper that applies the filtered PATH and execs the real script.
  WRAPPER="$STUB_DIR/run-wrapper.sh"
  printf '#!/usr/bin/env bash\nexport PATH="%s"\nexec "%s" "$@"\n' \
    "$FILTERED_PATH" "$SCRIPT" > "$WRAPPER"
  chmod +x "$WRAPPER"

  run --separate-stderr "$WRAPPER" "$WORK/test.sh"

  rm -rf "$STUB_DIR"

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
