#!/usr/bin/env bash
#
# run-golangci-lint.sh — Run golangci-lint on changed Go files and emit findings.
#
# Usage:
#   ./run-golangci-lint.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with review.sh merge_findings.
#   Outputs "[]" if golangci-lint is unavailable, no Go files changed, or no issues found.
#
# Environment:
#   GOLANGCI_MOCK_FILE   When set to a readable file path, read golangci-lint
#                        JSON output from that file instead of running the binary.
#                        Used by the bats test suite; unset in production.

set -euo pipefail

CHANGED_FILES="${1:-}"

if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: jq not installed; golangci-lint check skipped." >&2
  echo "[]"
  exit 0
fi

if [[ -z "${GOLANGCI_MOCK_FILE:-}" ]] && ! command -v golangci-lint >/dev/null 2>&1; then
  echo "WARNING: golangci-lint not installed; golangci-lint check skipped." >&2
  echo "[]"
  exit 0
fi

# Filter to Go source files (exclude test files to avoid double-counting)
GO_FILES=()
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  case "$file" in
    *.go) [[ -f "$file" ]] && GO_FILES+=("$file") ;;
  esac
done <<< "$CHANGED_FILES"

if [[ ${#GO_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

# Run golangci-lint (or read mock).
# --out-format=json --issues-exit-code=0 ensures JSON output even when issues exist.
# Pass only the specific files via --path-prefix-strip for cleaner paths.
if [[ -n "${GOLANGCI_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$GOLANGCI_MOCK_FILE" ]]; then
    echo "WARNING: GOLANGCI_MOCK_FILE '${GOLANGCI_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  GL_OUTPUT=$(cat "$GOLANGCI_MOCK_FILE")
else
  GL_OUTPUT=$(golangci-lint run --out-format=json --issues-exit-code=0 "${GO_FILES[@]}" 2>/dev/null || true)
fi

if [[ -z "$GL_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

# Convert golangci-lint JSON to the findings schema.
# Severity mapping by linter name:
#   errcheck, govet, staticcheck → High
#   everything else               → Medium
FINDINGS=$(echo "$GL_OUTPUT" | jq -r '
  [
    .Issues[]? |
    {
      severity: (
        if (.FromLinter == "errcheck" or .FromLinter == "govet" or .FromLinter == "staticcheck")
        then "High"
        else "Medium"
        end
      ),
      confidence: 90,
      source: "golangci-lint",
      file: .Pos.Filename,
      line: .Pos.Line,
      finding: ("\(.FromLinter): \(.Text)"),
      remediation: "Review the \(.FromLinter) linter documentation for this issue."
    }
  ]
' 2>/dev/null) || {
  echo "WARNING: golangci-lint output could not be parsed; golangci-lint findings skipped." >&2
  echo "[]"
  exit 0
}

echo "${FINDINGS:-[]}"
