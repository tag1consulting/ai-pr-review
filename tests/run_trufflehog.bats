#!/usr/bin/env bats
# Tests for run-trufflehog.sh. Uses TRUFFLEHOG_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-trufflehog.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/trufflehog"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "trufflehog: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "trufflehog: nonexistent file path returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/file.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "trufflehog: empty trufflehog output returns empty array" {
  touch "$WORK/settings.py"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-empty.json" run --separate-stderr "$SCRIPT" "$WORK/settings.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "trufflehog: verified secret maps to Critical confidence 95" {
  touch "$WORK/settings.py"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-verified.json" run --separate-stderr "$SCRIPT" "$WORK/settings.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
  echo "$output" | jq -e '.[0].severity == "Critical"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 95' > /dev/null
}

@test "trufflehog: unverified secret maps to High confidence 85" {
  touch "$WORK/deploy.sh"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-unverified.json" run --separate-stderr "$SCRIPT" "$WORK/deploy.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 85' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "trufflehog: findings conform to required schema" {
  touch "$WORK/settings.py"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-verified.json" run --separate-stderr "$SCRIPT" "$WORK/settings.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "trufflehog: source field is 'trufflehog' on all findings" {
  touch "$WORK/settings.py"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-verified.json" run --separate-stderr "$SCRIPT" "$WORK/settings.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "trufflehog")' > /dev/null
}

@test "trufflehog: finding text mentions detector name" {
  touch "$WORK/settings.py"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-verified.json" run --separate-stderr "$SCRIPT" "$WORK/settings.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("AWS")' > /dev/null
}

@test "trufflehog: remediation mentions rotating credentials" {
  touch "$WORK/settings.py"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-verified.json" run --separate-stderr "$SCRIPT" "$WORK/settings.py"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("[Rr]otate")' > /dev/null
}

# ---------------------------------------------------------------------------
# Test-file demotion — unverified secrets in test paths are demoted to Low
# ---------------------------------------------------------------------------

@test "trufflehog: unverified secret in test dir demoted to Low/40" {
  touch "$WORK/test.bats"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-unverified-test-file.json" run --separate-stderr "$SCRIPT" "$WORK/test.bats"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Low"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 40' > /dev/null
}

@test "trufflehog: unverified test-file finding tagged with mock data notice" {
  touch "$WORK/test.bats"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-unverified-test-file.json" run --separate-stderr "$SCRIPT" "$WORK/test.bats"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("test file")' > /dev/null
}

@test "trufflehog: unverified test-file remediation suggests verify instead of rotate" {
  touch "$WORK/test.bats"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-unverified-test-file.json" run --separate-stderr "$SCRIPT" "$WORK/test.bats"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("[Vv]erify")' > /dev/null
}

@test "trufflehog: verified secret in test dir stays Critical (never demoted)" {
  touch "$WORK/creds.json"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-verified-test-file.json" run --separate-stderr "$SCRIPT" "$WORK/creds.json"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "Critical"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 95' > /dev/null
}

@test "trufflehog: unverified secret in non-test path stays High" {
  touch "$WORK/deploy.sh"
  TRUFFLEHOG_MOCK_FILE="$FIXTURES/trufflehog-unverified.json" run --separate-stderr "$SCRIPT" "$WORK/deploy.sh"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].severity == "High"' > /dev/null
  echo "$output" | jq -e '.[0].confidence == 85' > /dev/null
  echo "$output" | jq -e '.[0].finding | test("test file") | not' > /dev/null
}
