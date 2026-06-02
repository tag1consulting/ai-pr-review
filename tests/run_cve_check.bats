#!/usr/bin/env bats
# Tests for run-cve-check.sh.
# Uses OSV_MOCK_FILE to bypass the network and feed canned responses.
#
# Mock file format: /v1/querybatch response, i.e. {"results": [{...}, ...]}.
# Each element in "results" corresponds to the package at the same index in
# the batch query. Missing indices return {} (treated as no vulns).

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

  # With batching, the mock returns one batch response for the entire set of
  # packages. The fixture has one result (index 0 = gin-gonic from go.mod), so
  # we expect at least one finding in total across both manifests.
  echo "$output" | jq -e 'length >= 1' > /dev/null
}

# ---------------------------------------------------------------------------
# Batched querybatch API — multi-package batch response
# ---------------------------------------------------------------------------

@test "cve-check: querybatch mock with multiple results produces multiple findings" {
  # go.mod.sample has 3 packages: gin (index 0), testify (index 1), crypto (index 2).
  # osv-batch-multi.json has findings at index 0 (gin) and index 2 (crypto), empty at 1.
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-batch-multi.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'type == "array" and length >= 2' > /dev/null
  # First finding is for gin-gonic (Critical)
  echo "$output" | jq -e '[.[] | select(.finding | test("CVE-2025-99999"))] | length >= 1' > /dev/null
  # Second finding is for crypto (High)
  echo "$output" | jq -e '[.[] | select(.finding | test("CVE-2024-88888"))] | length >= 1' > /dev/null
  # No finding for testify (index 1 returned empty)
  echo "$output" | jq -e '[.[] | select(.finding | test("testify"))] | length == 0' > /dev/null
}

@test "cve-check: querybatch stderr reports attempt and check counts" {
  cp "$FIXTURES/go.mod.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-batch-multi.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  # Should report queried N/M where M >= 3 (the number of packages in go.mod.sample)
  [[ "$stderr" == *"CVE check: queried"* ]]
}

# ---------------------------------------------------------------------------
# go.mod replace directive handling
# ---------------------------------------------------------------------------

@test "cve-check: go.mod replace directive maps module to replacement coordinates" {
  # go.mod.replace.sample has:
  #   require github.com/old-module/lib v0.5.0
  #   replace github.com/old-module/lib => github.com/new-module/lib v0.9.0
  # The parser should query new-module/lib, not old-module/lib.
  cp "$FIXTURES/go.mod.replace.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-empty.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
  # Two packages queried: gin (not replaced) + new-module/lib (replacement).
  # local-only/thing is replaced by a local path and must be skipped.
  [[ "$stderr" == *"CVE check: queried"* ]]
  # Verify the attempt count is 2 (not 3), confirming local-path replace was skipped.
  local attempted
  attempted=$(echo "$stderr" | grep -oE 'queried [0-9]+/[0-9]+' | grep -oE '[0-9]+$')
  [ "$attempted" -eq 2 ]
}

@test "cve-check: go.mod local-path replace is skipped entirely" {
  # go.mod.replace.sample replaces github.com/local-only/thing => ./internal/thing.
  # That entry must not appear in the query set (local paths are not in OSV).
  cp "$FIXTURES/go.mod.replace.sample" "$WORK/go.mod"
  OSV_MOCK_FILE="$FIXTURES/osv-empty.json" run --separate-stderr "$SCRIPT" "$WORK/go.mod"
  [ "$status" -eq 0 ]
  # Attempted count must be 2 (gin + new-module/lib), not 3.
  local attempted
  attempted=$(echo "$stderr" | grep -oE 'queried [0-9]+/[0-9]+' | grep -oE '[0-9]+$')
  [ "$attempted" -eq 2 ]
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
