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

# Test/fixture file pattern — unverified findings in these paths are demoted
# to Low because test files routinely contain fake credentials for mocking.
# Verified secrets are never demoted (a real leaked credential is critical
# regardless of where it appears).
# Matches: test directories (tests/, __tests__/, spec/, fixtures/, testdata/,
# mocks/, stubs/, fakes/), test file naming conventions (*_test.go, test_*.py,
# *.test.ts, *.spec.ts, *.bats), and example/sample directories.
_TEST_FILE_PATTERN='(^|/)(tests?|__tests__|spec|fixtures?|testdata|test_data|mocks?|stubs?|fakes?|examples?|samples?)/|_test\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$|\.bats$|^test_[^/]+\.[a-z]+$|(^|/)test_[^/]+\.[a-z]+$'

# jq filter to convert trufflehog NDJSON into findings array
_th_transform() {
  local test_pattern="${_TEST_FILE_PATTERN}"
  jq -Rs --arg test_pat "$test_pattern" '
    split("\n") | map(select(length > 0)) |
    map(
      (. | fromjson? // null) |
      select(. != null) |
      (.SourceMetadata.Data.Filesystem.file? // "unknown") as $file |
      (.Verified) as $verified |
      (if ($verified | not) and ($file | test($test_pat)) then true else false end) as $is_test_fp |
      {
        severity: (
          if $verified then "Critical"
          elif $is_test_fp then "Low"
          else "High"
          end
        ),
        confidence: (
          if $verified then 95
          elif $is_test_fp then 40
          else 85
          end
        ),
        source: "trufflehog",
        file: $file,
        line: (.SourceMetadata.Data.Filesystem.line? // 0),
        finding: (
          "Potential secret detected: \(.DetectorName) (\(if $verified then "verified" else "unverified" end))"
          + (if $is_test_fp then " [test file — likely mock data]" else "" end)
        ),
        remediation: (
          if $is_test_fp then
            "Verify this is intentional test/mock data. If it is a real credential, rotate it immediately."
          else
            "Rotate the credential immediately and remove it from the repository history."
          end
        )
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

# Production path: trufflehog filesystem accepts multiple paths in a single
# invocation, so pass all target files at once rather than forking per file.
# On PRs touching many files this avoids N-1 process startups.
TH_OUTPUT=$(trufflehog filesystem --json --no-update "${TARGET_FILES[@]}" 2>/dev/null || true)

if [[ -z "$TH_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

FINDINGS=$(echo "$TH_OUTPUT" | _th_transform 2>/dev/null) || {
  echo "WARNING: trufflehog output could not be parsed; trufflehog findings skipped." >&2
  echo "[]"
  exit 0
}

echo "${FINDINGS:-[]}"
