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

@test "checkov: k8s YAML with core API group (v1) triggers scan" {
  cat > "$WORK/svc.yaml" <<'EOF'
apiVersion: v1
kind: Service
metadata:
  name: demo
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/svc.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: k8s YAML with beta API group triggers scan" {
  cat > "$WORK/ingress.yaml" <<'EOF'
apiVersion: networking.k8s.io/v1beta1
kind: Ingress
metadata:
  name: demo
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/ingress.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: k8s YAML with quoted apiVersion triggers scan" {
  # Some tooling emits apiVersion with quotes; the portable (no-backreference)
  # pattern must handle both shapes.
  cat > "$WORK/quoted.yaml" <<'EOF'
apiVersion: "apps/v1"
kind: Deployment
metadata:
  name: demo
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/quoted.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: OpenAPI spec with apiVersion+kind (non-k8s shape) is skipped" {
  # OpenAPI specs and similar tooling configs can use both apiVersion and
  # kind keys without being k8s. The apiVersion value here is free-form
  # text, not a k8s-shaped version string.
  cat > "$WORK/openapi.yaml" <<'EOF'
apiVersion: "2025-01-01"
kind: openapi-extension
info:
  title: My API
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/openapi.yaml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "checkov: custom tooling YAML with non-k8s apiVersion is skipped" {
  cat > "$WORK/tool.yaml" <<'EOF'
apiVersion: mytool/2024.03
kind: Config
settings:
  foo: bar
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/tool.yaml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
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

@test "checkov: Azure ARM YAML triggers scan" {
  cat > "$WORK/arm.yaml" <<'EOF'
$schema: https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#
contentVersion: 1.0.0.0
resources: []
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/arm.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: Azure ARM JSON triggers scan" {
  cat > "$WORK/arm.json" <<'EOF'
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "resources": []
}
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/arm.json"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

@test "checkov: JSON containing AWSTemplateFormatVersion as bare substring is skipped" {
  # Regression guard: the JSON filter must anchor on the key shape, not
  # match the bare string anywhere in the file. Otherwise a docs fixture
  # or dependency name could falsely trigger checkov.
  cat > "$WORK/pkg.json" <<'EOF'
{
  "name": "some-pkg",
  "description": "mentions AWSTemplateFormatVersion in prose without being a CFN template"
}
EOF
  CHECKOV_MOCK_FILE="$FIXTURES/checkov-failed.json" run --separate-stderr "$SCRIPT" "$WORK/pkg.json"
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
