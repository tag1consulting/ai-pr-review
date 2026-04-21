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

FINDINGS="[]"

for file in "${TARGET_FILES[@]}"; do
  # Run trufflehog (or read mock) — filesystem mode, JSON output, one line per result
  if [[ -n "${TRUFFLEHOG_MOCK_FILE:-}" ]]; then
    if [[ ! -r "$TRUFFLEHOG_MOCK_FILE" ]]; then
      echo "WARNING: TRUFFLEHOG_MOCK_FILE '${TRUFFLEHOG_MOCK_FILE}' is not readable." >&2
      continue
    fi
    TH_OUTPUT=$(cat "$TRUFFLEHOG_MOCK_FILE")
  else
    TH_OUTPUT=$(trufflehog filesystem --json --no-update "$file" 2>/dev/null || true)
  fi

  [[ -z "$TH_OUTPUT" ]] && continue

  # trufflehog emits one JSON object per line (NDJSON); wrap into array
  FILE_FINDINGS=$(echo "$TH_OUTPUT" | jq -Rs '
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
  ' 2>/dev/null) || {
    echo "WARNING: trufflehog output for ${file} could not be parsed; skipping." >&2
    continue
  }

  FINDINGS=$(echo "$FINDINGS" "$FILE_FINDINGS" | jq -s '.[0] + .[1]' 2>/dev/null) || true
done

echo "${FINDINGS:-[]}"
