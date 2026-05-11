#!/usr/bin/env bats
# Tests for the apply_suppressions() pipeline in lib/findings.sh.
#
# Each test invokes a harness script (written once per test run) that stubs
# all globals and runs the function in a subprocess.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  WORK=$(mktemp -d)
  FINDINGS_FILE="${WORK}/findings.json"
  SUPPRESSION_DIR="${WORK}/suppdir"
  mkdir -p "$SUPPRESSION_DIR"

  # Write a reusable harness script that can be sourced per-test
  HARNESS="${WORK}/harness.sh"
  cat > "$HARNESS" <<HARNESS_EOF
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="\${1}"
FINDINGS_JSON_FILE="\${2}"
TMPFILES=()
GITHUB_WORKSPACE=""
SUPPRESSED_COUNT=0
source "${PROJECT_ROOT}/lib/findings.sh"
apply_suppressions
cat "\${FINDINGS_JSON_FILE}"
HARNESS_EOF
  chmod +x "$HARNESS"
}

teardown() {
  rm -rf "$WORK"
}

# Write suppressions.json for this test
_write_suppressions() {
  mkdir -p "${SUPPRESSION_DIR}/config"
  printf '%s\n' "$1" > "${SUPPRESSION_DIR}/config/suppressions.json"
}

# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

@test "apply_suppressions: no-op when suppressions.json missing" {
  echo '[{"severity":"High","confidence":90,"source":"code-reviewer","file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_FILE"
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 1 ]
}

@test "apply_suppressions: no-op when suppressions.json is empty array" {
  _write_suppressions '[]'
  echo '[{"severity":"High","confidence":90,"source":"code-reviewer","file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_FILE"
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 1 ]
}

@test "apply_suppressions: no-op when no findings match any rule" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"pattern":"NOMATCH"}}]'
  echo '[{"severity":"High","confidence":90,"source":"code-reviewer","file":"a.sh","line":1,"finding":"real issue","remediation":"y"}]' \
    > "$FINDINGS_FILE"
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Pattern match
# ---------------------------------------------------------------------------

@test "apply_suppressions: suppresses finding matching pattern rule" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"pattern":"SC1234"}}]'
  echo '[{"severity":"Medium","confidence":90,"source":"shellcheck","file":"a.sh","line":5,"finding":"SC1234: some message","remediation":"y"}]' \
    > "$FINDINGS_FILE"
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 0 ]
}

@test "apply_suppressions: pattern match is case-insensitive" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"pattern":"false positive"}}]'
  echo '[{"severity":"Medium","confidence":90,"source":"code-reviewer","file":"a.sh","line":5,"finding":"False Positive example","remediation":"y"}]' \
    > "$FINDINGS_FILE"
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 0 ]
}

# ---------------------------------------------------------------------------
# File match
# ---------------------------------------------------------------------------

@test "apply_suppressions: suppresses finding matching file substring" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"file":"vendor/"}}]'
  cat > "$FINDINGS_FILE" <<'EOF'
[
  {"severity":"High","confidence":90,"source":"code-reviewer","file":"vendor/lib.sh","line":1,"finding":"issue","remediation":"y"},
  {"severity":"High","confidence":90,"source":"code-reviewer","file":"src/main.sh","line":1,"finding":"issue","remediation":"y"}
]
EOF
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 1 ]
  file=$(echo "$result" | jq -r '.[0].file')
  [ "$file" = "src/main.sh" ]
}

# ---------------------------------------------------------------------------
# Code prefix match
# ---------------------------------------------------------------------------

@test "apply_suppressions: suppresses finding matching code prefix" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"code":"SC2016"}}]'
  echo '[{"severity":"Low","confidence":90,"source":"shellcheck","file":"a.sh","line":1,"finding":"SC2016: do not expand","remediation":"y"}]' \
    > "$FINDINGS_FILE"
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 0 ]
}

# ---------------------------------------------------------------------------
# source field is irrelevant to suppression matching
# ---------------------------------------------------------------------------

@test "apply_suppressions: suppresses matching findings regardless of source" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"pattern":"SC1234"}}]'
  cat > "$FINDINGS_FILE" <<'EOF'
[
  {"severity":"Medium","confidence":90,"source":"shellcheck","file":"a.sh","line":5,"finding":"SC1234: from shellcheck","remediation":"y"},
  {"severity":"Medium","confidence":90,"source":"code-reviewer","file":"a.sh","line":6,"finding":"SC1234: from llm","remediation":"y"}
]
EOF
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 0 ]
}

@test "apply_suppressions: keeps non-matching findings regardless of source" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"pattern":"SC1234"}}]'
  cat > "$FINDINGS_FILE" <<'EOF'
[
  {"severity":"High","confidence":90,"source":"shellcheck","file":"a.sh","line":5,"finding":"SC9999: different","remediation":"y"},
  {"severity":"High","confidence":90,"source":"code-reviewer","file":"a.sh","line":6,"finding":"unrelated issue","remediation":"y"}
]
EOF
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 2 ]
}

# ---------------------------------------------------------------------------
# Mixed keep/suppress
# ---------------------------------------------------------------------------

@test "apply_suppressions: partial suppression keeps non-matching findings" {
  _write_suppressions '[{"id":"r1","reason":"test","match":{"pattern":"suppress me"}}]'
  cat > "$FINDINGS_FILE" <<'EOF'
[
  {"severity":"High","confidence":90,"source":"code-reviewer","file":"a.sh","line":1,"finding":"suppress me please","remediation":"y"},
  {"severity":"High","confidence":90,"source":"code-reviewer","file":"a.sh","line":2,"finding":"keep this one","remediation":"y"}
]
EOF
  result=$("$HARNESS" "$SUPPRESSION_DIR" "$FINDINGS_FILE")
  count=$(echo "$result" | jq 'length')
  [ "$count" -eq 1 ]
  finding=$(echo "$result" | jq -r '.[0].finding')
  [ "$finding" = "keep this one" ]
}
