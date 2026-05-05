#!/usr/bin/env bash
#
# run-trufflehog.sh — Run trufflehog on changed files and emit findings.
#
# Usage:
#   ./run-trufflehog.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with review.sh merge_findings.
#   Outputs "[]" if trufflehog is unavailable, no files match, or no secrets found.
#
# Environment:
#   TRUFFLEHOG_MOCK_FILE  When set to a readable file path, read trufflehog
#                         JSON output from that file instead of running the
#                         binary. Used by the bats test suite; unset in production.

set -euo pipefail

CHANGED_FILES="${1:-}"

if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: jq not installed; trufflehog check skipped." >&2
  echo "[]"
  exit 0
fi

if [[ -z "${TRUFFLEHOG_MOCK_FILE:-}" ]] && ! command -v trufflehog >/dev/null 2>&1; then
  echo "WARNING: trufflehog not installed; trufflehog check skipped." >&2
  echo "[]"
  exit 0
fi

# Collect files that exist on disk
TARGET_FILES=()
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  [[ -f "$file" ]] && TARGET_FILES+=("$file")
done <<< "$CHANGED_FILES"

if [[ ${#TARGET_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

# jq filter to convert trufflehog NDJSON into findings array
_th_transform() {
  jq -Rs '
    split("\n") | map(select(length > 0)) |
    map(
      (. | fromjson? // null) |
      select(. != null) |
      {
        severity: (if .Verified then "Critical" else "High" end),
        confidence: (if .Verified then 95 else 85 end),
        source: "trufflehog",
        file: (.SourceMetadata.Data.Filesystem.file? // "unknown"),
        line: (.SourceMetadata.Data.Filesystem.line? // 0),
        finding: ("Potential secret detected: \(.DetectorName) (\(if .Verified then "verified" else "unverified" end))"),
        remediation: "Rotate the credential immediately and remove it from the repository history."
      }
    )
  '
}

# Mock path: read once — the fixture represents the full tool output, not per-file output
if [[ -n "${TRUFFLEHOG_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$TRUFFLEHOG_MOCK_FILE" ]]; then
    echo "WARNING: TRUFFLEHOG_MOCK_FILE '${TRUFFLEHOG_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  FINDINGS=$(cat "$TRUFFLEHOG_MOCK_FILE" | _th_transform 2>/dev/null) || {
    echo "WARNING: TRUFFLEHOG_MOCK_FILE could not be parsed." >&2
    echo "[]"
    exit 0
  }
  echo "${FINDINGS:-[]}"
  exit 0
fi

# Production path: run trufflehog once per file, collect arrays, merge once at the end
FILE_ARRAYS=()
for file in "${TARGET_FILES[@]}"; do
  TH_OUTPUT=$(trufflehog filesystem --json --no-update "$file" 2>/dev/null || true)
  [[ -z "$TH_OUTPUT" ]] && continue

  FILE_FINDINGS=$(echo "$TH_OUTPUT" | _th_transform 2>/dev/null) || {
    echo "WARNING: trufflehog output for ${file} could not be parsed; skipping." >&2
    continue
  }

  FILE_ARRAYS+=("$FILE_FINDINGS")
done

if [[ ${#FILE_ARRAYS[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

# Single merge: concatenate all per-file arrays and flatten
printf '%s\n' "${FILE_ARRAYS[@]}" | jq -s 'add // []'
