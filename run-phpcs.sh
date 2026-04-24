#!/usr/bin/env bash
#
# run-phpcs.sh — Run phpcs (PHP_CodeSniffer) on changed PHP files and emit findings.
#
# Uses the Drupal and DrupalPractice coding standards when available via
# drupal/coder, otherwise falls back to PSR12.
#
# Usage:
#   ./run-phpcs.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with review.sh merge_findings.
#   Outputs "[]" if phpcs is unavailable, no PHP files changed, or no issues found.
#
# Environment:
#   PHPCS_MOCK_FILE   When set to a readable file path, read phpcs JSON
#                     output from that file instead of running the binary.
#                     Used by the bats test suite; unset in production.

set -euo pipefail

CHANGED_FILES="${1:-}"

if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: jq not installed; phpcs check skipped." >&2
  echo "[]"
  exit 0
fi

if [[ -z "${PHPCS_MOCK_FILE:-}" ]] && ! command -v phpcs >/dev/null 2>&1; then
  echo "WARNING: phpcs not installed; phpcs check skipped." >&2
  echo "[]"
  exit 0
fi

# Filter to PHP files only
PHP_FILES=()
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  case "$file" in
    *.php|*.module|*.inc|*.theme|*.install|*.profile)
      [[ -f "$file" ]] && PHP_FILES+=("$file") ;;
  esac
done <<< "$CHANGED_FILES"

if [[ ${#PHP_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

if [[ -n "${PHPCS_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$PHPCS_MOCK_FILE" ]]; then
    echo "WARNING: PHPCS_MOCK_FILE '${PHPCS_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  PHPCS_OUTPUT=$(cat "$PHPCS_MOCK_FILE")
else
  # Select standard: prefer Drupal,DrupalPractice (via drupal/coder), fall back to PSR12
  PHPCS_STANDARD="PSR12"
  if phpcs -i 2>/dev/null | grep -q "Drupal"; then
    PHPCS_STANDARD="Drupal,DrupalPractice"
  fi

  # --report=json emits structured output; -q suppresses progress; --runtime-set
  # ignore_warnings_on_exit 1 ensures non-zero exit only on errors (not warnings),
  # but we use || true anyway since any findings produce exit 1
  PHPCS_OUTPUT=$(phpcs \
    --report=json \
    --standard="$PHPCS_STANDARD" \
    --extensions=php,module,inc,theme,install,profile \
    -q \
    "${PHP_FILES[@]}" \
    2>/dev/null) || true
fi

if [[ -z "$PHPCS_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

# phpcs JSON structure:
# {
#   "files": {
#     "/path/to/file.php": {
#       "errors": 2, "warnings": 1,
#       "messages": [
#         {"message":"...", "source":"Drupal.Commenting.DocComment", "severity":5, "type":"ERROR", "line":10, "column":1, "fixable":false}
#       ]
#     }
#   }
# }
# Severity mapping:
#   type == "ERROR"   → High
#   type == "WARNING" → Medium
FINDINGS=$(echo "$PHPCS_OUTPUT" | jq -r '
  [
    .files // {} |
    to_entries[] |
    . as $entry |
    .value.messages[]? |
    {
      severity: (if .type == "ERROR" then "High" else "Medium" end),
      confidence: 90,
      source: "phpcs",
      file: $entry.key,
      line: (.line // 1),
      finding: ("\(.source): \(.message)"),
      remediation: (
        "See https://www.drupal.org/docs/develop/standards or fix with: phpcs --standard=Drupal \($entry.key)"
      )
    }
  ]
' 2>/dev/null) || {
  echo "WARNING: phpcs output could not be parsed; phpcs findings skipped." >&2
  echo "[]"
  exit 0
}

echo "${FINDINGS:-[]}"
