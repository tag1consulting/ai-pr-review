#!/usr/bin/env bats
# Tests for run-checkov.sh. Uses CHECKOV_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-checkov.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/checkov"
  WORK=$(mktemp -d)
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "checkov: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: non-IaC file returns empty array" {
  touch "$WORK/main.py"
  run --separate-stderr "$SCRIPT" "$WORK/main.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: nonexistent file returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/main.tf"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: empty results returns empty array" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-empty.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: malformed output falls through safely" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# File extension matching
# ---------------------------------------------------------------------------

@test "checkov: .tf file triggers scan" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: k8s YAML (apiVersion + kind) triggers scan" {
  cat > "$WORK/deployment.yaml" <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: CloudFormation YAML triggers scan" {
  cat > "$WORK/stack.yaml" <<'EOF'
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  Bucket:
    Type: AWS::S3::Bucket
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/stack.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: CloudFormation JSON triggers scan" {
  cat > "$WORK/stack.json" <<'EOF'
{"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/stack.json"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: non-IaC YAML (docker-compose) is skipped" {
  cat > "$WORK/docker-compose.yml" <<'EOF'
version: '3'
services:
  web:
    image: nginx
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/docker-compose.yml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: non-IaC YAML (GitHub Actions workflow) is skipped" {
  cat > "$WORK/ci.yml" <<'EOF'
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/ci.yml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: non-IaC JSON (package-lock) is skipped" {
  echo '{"name": "demo", "lockfileVersion": 3}' > "$WORK/package-lock.json"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/package-lock.json"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: Dockerfile triggers scan" {
  touch "$WORK/Dockerfile"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/Dockerfile"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "checkov: findings conform to required schema" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "checkov: source field is 'checkov' on all findings" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "checkov")' > /dev/null
}

@test "checkov: confidence is 80 on all findings" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 80)' > /dev/null
}

@test "checkov: finding text contains check ID" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("CKV_AWS_57")' > /dev/null
}

@test "checkov: remediation contains guideline URL when present" {
  touch "$WORK/main.tf"
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/main.tf"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | test("aws.amazon.com")' > /dev/null
}
