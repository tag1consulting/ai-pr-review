#!/usr/bin/env bash
#
# run-shellcheck.sh — Run shellcheck on changed .sh files and output findings.
#
# Usage:
#   ./run-shellcheck.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with post-review.sh json-findings format.
#   Outputs "[]" if no issues found or shellcheck is not available.

set -euo pipefail

CHANGED_FILES="$1"

# Check if shellcheck is available
if ! command -v shellcheck &> /dev/null; then
  echo "WARNING: shellcheck not installed, skipping lint pass." >&2
  echo "[]"
  exit 0
fi

# Filter to .sh and .bash files
SHELL_FILES=()
while IFS= read -r file; do
  case "$file" in
    *.sh|*.bash) SHELL_FILES+=("$file") ;;
  esac
done <<< "$CHANGED_FILES"

if [[ ${#SHELL_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

# Run shellcheck on each file, collect JSON output
FINDINGS="[]"
for file in "${SHELL_FILES[@]}"; do
  [[ ! -f "$file" ]] && continue

  # shellcheck outputs JSON with -f json1
  SC_OUTPUT=$(shellcheck -f json1 -S warning -- "$file" 2>/dev/null || true)

  if [[ -z "$SC_OUTPUT" ]]; then
    continue
  fi

  # Parse shellcheck JSON and convert to our findings format
  FILE_FINDINGS=$(echo "$SC_OUTPUT" | jq -r --arg file "$file" '
    [.comments[]? | select(.level == "warning" or .level == "error") | {
      severity: (if .level == "error" then "High" else "Medium" end),
      confidence: 95,
      file: $file,
      line: .line,
      finding: ("SC\(.code): \(.message)"),
      remediation: ("See https://www.shellcheck.net/wiki/SC\(.code)")
    }]
  ' 2>/dev/null || echo "[]")

  FINDINGS=$(echo "$FINDINGS" "$FILE_FINDINGS" | jq -s '.[0] + .[1]')
done

printf '%s\n' "$FINDINGS"
