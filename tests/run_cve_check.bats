#!/usr/bin/env bats
# Tests for run-cve-check.sh.
# Uses OSV_MOCK_FILE to bypass the network and feed canned responses.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-cve-check.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/cve"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "cve-check: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "cve-check: no manifest files in input returns empty array" {
  run --separate-stderr "$SCRIPT" $'src/foo.go\nREADME.md'
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "cve-check: nonexistent manifest paths return empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/go.mod"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Go manifest with critical CVE
# ---------------------------------------------------------------------------

@test "cve-check: go.mod with vulnerable version produces Critical finding" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-critical.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'type == "array" and length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "Critical"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 95' > /dev/null
  echo "$output" | jq -e '.[0].file | endswith("go.mod")' > /dev/null
  echo "$output" | jq -e '.[0].finding | test("CVE-2025-99999")' > /dev/null
  echo "$output" | jq -e '.[0].remediation | test("1.7.7")' > /dev/null
}

@test "cve-check: go.mod finding line number is non-zero" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-critical.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  local line
  line=$(echo "$output" | jq '.[0].line')
  [ "$line" -ge 1 ]
}

# ---------------------------------------------------------------------------
# package.json with medium CVE
# ---------------------------------------------------------------------------

@test "cve-check: package.json with medium CVSS produces Medium finding" {
  cp "$FIXTURES/package.json.sample" "$WORK/package.json"
  OSV_MOCK_FILE="$FIXTURES/osv-medium.json" run --separate-stderr "$SCRIPT" "$WORK/package.json"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "Medium"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 90' > /dev/null
}

# ---------------------------------------------------------------------------
# Clean manifests
# ---------------------------------------------------------------------------

@test "cve-check: empty OSV response returns empty findings array" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-empty.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "cve-check: malformed OSV response falls through safely" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Manifest parsing (structural, without vulnerabilities)
# ---------------------------------------------------------------------------

@test "cve-check: requirements.txt is parsed (emits empty when OSV clean)" {
  cp "$FIXTURES/requirements.txt.sample" "$WORK/requirements.txt"
  OSV_MOCK_FILE="$FIXTURES/osv-empty.json" run --separate-stderr "$SCRIPT" "$WORK/requirements.txt"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
  [[ "$stderr" == *"CVE check: queried"* ]]
}

@test "cve-check: composer.json is parsed (emits empty when OSV clean)" {
  cp "$FIXTURES/composer.json.sample" "$WORK/composer.json"
  OSV_MOCK_FILE="$FIXTURES/osv-empty.json" run --separate-stderr "$SCRIPT" "$WORK/composer.json"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Multiple manifests in one run
# ---------------------------------------------------------------------------

@test "cve-check: multiple manifests in one invocation" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  cp "$FIXTURES/package.json.sample" "$WORK/package.json"
  OSV_MOCK_FILE="$FIXTURES/osv-critical.json" run --separate-stderr "$SCRIPT" "${WORK}/go.mod"$'\n'"${WORK}/package.json"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'type == "array"' > /dev/null

  # OSV_MOCK_FILE returns the same response for every queried package, so one
  # finding is emitted per (manifest, package) pair. Assert that findings are
  # present for both manifests rather than just count >= 1.
  local go_count pkg_count
  go_count=$(echo "$output" | jq '[.[] | select(.file | endswith("go.mod"))] | length')
  pkg_count=$(echo "$output" | jq '[.[] | select(.file | endswith("package.json"))] | length')
  [ "$go_count" -ge 1 ]
  [ "$pkg_count" -ge 1 ]
}

# ---------------------------------------------------------------------------
# Output schema conformance
# ---------------------------------------------------------------------------

@test "cve-check: findings conform to required schema" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-critical.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source") and has("file")
        and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
  echo "$output" | jq -e '
    all(.[]; .severity | IN("Critical","High","Medium","Low"))
  ' > /dev/null
  echo "$output" | jq -e '
    all(.[]; .confidence >= 75 and .confidence <= 100)
  ' > /dev/null
}

@test "cve-check: source field is 'osv' on all findings" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-critical.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "osv")' > /dev/null
}
