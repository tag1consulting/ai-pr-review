#!/usr/bin/env bats
# Tests for run-kube-linter.sh. Uses KUBELINTER_MOCK_FILE to bypass the binary.

bats_require_minimum_version 1.5.0

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  SCRIPT="${PROJECT_ROOT}/analyzers/run-kube-linter.sh"
  FIXTURES="${PROJECT_ROOT}/tests/fixtures/kubelinter"
  WORK=$(mktemp -d)
  # Create a minimal Kubernetes manifest for heuristic detection
  cat > "$WORK/deployment.yaml" <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
spec:
  template:
    spec:
      containers:
        - name: nginx
          image: nginx:latest
YAML
}

teardown() {
  rm -rf "$WORK"
}

# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@test "kube-linter: empty input returns empty array" {
  run --separate-stderr "$SCRIPT" ""
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "kube-linter: non-YAML file returns empty array" {
  touch "$WORK/main.py"
  run --separate-stderr "$SCRIPT" "$WORK/main.py"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "kube-linter: YAML without apiVersion+kind returns empty array" {
  # A plain YAML config that is not a K8s manifest
  printf 'foo: bar\nbaz: qux\n' > "$WORK/config.yaml"
  run --separate-stderr "$SCRIPT" "$WORK/config.yaml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "kube-linter: nonexistent file returns empty array" {
  run --separate-stderr "$SCRIPT" "nonexistent/deployment.yaml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "kube-linter: empty violations returns empty array" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-empty.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

@test "kube-linter: malformed output falls through safely" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-malformed.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  [ "$output" = "[]" ]
}

# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------

@test "kube-linter: K8s manifest with apiVersion+kind triggers scan" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'length > 0' > /dev/null
}

# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

@test "kube-linter: all findings map to Medium severity" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .severity == "Medium")' > /dev/null
}

# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------

@test "kube-linter: findings conform to required schema" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '
    all(.[]; has("severity") and has("confidence") and has("source")
        and has("file") and has("line") and has("finding") and has("remediation"))
  ' > /dev/null
}

@test "kube-linter: source field is 'kube-linter' on all findings" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .source == "kube-linter")' > /dev/null
}

@test "kube-linter: confidence is 85 on all findings" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e 'all(.[]; .confidence == 85)' > /dev/null
}

@test "kube-linter: finding text contains check name" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("no-read-only-root-fs")' > /dev/null
}

@test "kube-linter: finding text contains resource kind and name" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].finding | test("Deployment")' > /dev/null
}

@test "kube-linter: remediation is populated" {
  KUBELINTER_MOCK_FILE="$FIXTURES/kubelinter-violations.json" run --separate-stderr "$SCRIPT" "$WORK/deployment.yaml"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.[0].remediation | length > 0' > /dev/null
}
