#!/usr/bin/env bash
#
# run-checkov.sh — Run checkov on changed IaC files and emit findings.
#
# Covers: Terraform (*.tf, *.tfvars), Kubernetes/Helm YAML, Dockerfiles,
# CloudFormation JSON/YAML, GitHub Actions workflows.
#
# Usage:
#   ./run-checkov.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with review.sh merge_findings.
#   Outputs "[]" if checkov is unavailable, no IaC files changed, or no issues found.
#
# Environment:
#   CHECKOV_MOCK_FILE   When set to a readable file path, read checkov JSON
#                       output from that file instead of running the binary.
#                       Used by the bats test suite; unset in production.

set -euo pipefail

CHANGED_FILES="${1:-}"

if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: jq not installed; checkov check skipped." >&2
  echo "[]"
  exit 0
fi

if [[ -z "${CHECKOV_MOCK_FILE:-}" ]] && ! command -v checkov >/dev/null 2>&1; then
  echo "WARNING: checkov not installed; checkov check skipped." >&2
  echo "[]"
  exit 0
fi

# Collect IaC files that exist on disk
IAC_FILES=()
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  [[ -f "$file" ]] || continue
  case "$file" in
    *.tf|*.tfvars) IAC_FILES+=("$file") ;;
    *.yaml|*.yml)  IAC_FILES+=("$file") ;;
    Dockerfile|*/Dockerfile|Dockerfile.*|*/Dockerfile.*|*.dockerfile) IAC_FILES+=("$file") ;;
    *.json)        IAC_FILES+=("$file") ;;
  esac
done <<< "$CHANGED_FILES"

if [[ ${#IAC_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

if [[ -n "${CHECKOV_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$CHECKOV_MOCK_FILE" ]]; then
    echo "WARNING: CHECKOV_MOCK_FILE '${CHECKOV_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  CHECKOV_OUTPUT=$(cat "$CHECKOV_MOCK_FILE")
else
  # Build --file arg list: checkov accepts repeated --file flags
  CHECKOV_FILE_ARGS=()
  for f in "${IAC_FILES[@]}"; do
    CHECKOV_FILE_ARGS+=(--file "$f")
  done

  # --output json --quiet suppresses progress output; --compact omits passing checks
  # checkov exits 1 when findings are found (expected); exit ≥2 indicates a real error.
  CHECKOV_EC=0
  CHECKOV_OUTPUT=$(checkov \
    "${CHECKOV_FILE_ARGS[@]}" \
    --output json \
    --quiet \
    --compact \
    2>/dev/null) || CHECKOV_EC=$?
  if [[ "$CHECKOV_EC" -ge 2 ]]; then
    echo "WARNING: checkov exited with error code ${CHECKOV_EC}; checkov may not be installed correctly." >&2
    echo "[]"
    exit 0
  fi

  if [[ -z "$CHECKOV_OUTPUT" ]]; then
    echo "[]"
    exit 0
  fi
fi

if [[ -z "$CHECKOV_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

# checkov JSON can be a single object or an array of objects (one per framework).
# Normalise to an array, extract failed_checks from each, project to findings schema.
# Severity mapping: checkov has no severity field on its checks; map by check_id prefix:
#   CKV2_* (v2 rules) and CKV_SECRET_* (secret detection) → High
#   All other checks → Medium
# We use confidence 80 (static analysis without runtime context).
FINDINGS=$(echo "$CHECKOV_OUTPUT" | jq -r '
  (if type == "array" then . else [.] end) |
  [
    .[].results?.failed_checks[]? |
    {
      severity: (
        if (.check_id | test("^(CKV2_|CKV_SECRET_)")) then "High"
        else "Medium"
        end
      ),
      confidence: 80,
      source: "checkov",
      file: (.repo_file_path | ltrimstr("/")),
      line: (.file_line_range[0] // 1),
      finding: ("\(.check_id): \(.check_id_name // .resource // "policy violation")"),
      remediation: (
        if .guideline and (.guideline | length > 0) then .guideline
        else "See https://docs.prismacloud.io/en/enterprise-edition/policy-reference"
        end
      )
    }
  ]
' 2>/dev/null) || {
  echo "WARNING: checkov output could not be parsed; checkov findings skipped." >&2
  echo "[]"
  exit 0
}

echo "${FINDINGS:-[]}"
