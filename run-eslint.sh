#!/usr/bin/env bash
#
# run-eslint.sh — Run ESLint on changed JS/TS files and emit findings.
#
# Requires that the consuming repository already has ESLint configured
# (eslint.config.* or .eslintrc.*) and accessible via `npx eslint` or a
# locally-installed `./node_modules/.bin/eslint`. The script is a no-op if
# no ESLint config is found, so consumers without JS/TS code are unaffected.
#
# Usage:
#   ./run-eslint.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with review.sh merge_findings.
#   Outputs "[]" if ESLint is unavailable, no config found, no JS/TS files
#   changed, or no issues found.
#
# Environment:
#   ESLINT_MOCK_FILE   When set to a readable file path, read ESLint JSON
#                      output from that file instead of running the binary.
#                      Used by the bats test suite; unset in production.

set -euo pipefail

CHANGED_FILES="${1:-}"

if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: jq not installed; eslint check skipped." >&2
  echo "[]"
  exit 0
fi

# Resolve eslint binary: prefer local node_modules, fall back to npx
ESLINT_BIN=""
if [[ -z "${ESLINT_MOCK_FILE:-}" ]]; then
  if [[ -x "./node_modules/.bin/eslint" ]]; then
    ESLINT_BIN="./node_modules/.bin/eslint"
  elif command -v npx >/dev/null 2>&1 && npx --no eslint --version >/dev/null 2>&1; then
    ESLINT_BIN="npx eslint"
  else
    echo "WARNING: eslint not found (tried node_modules/.bin/eslint and npx); eslint check skipped." >&2
    echo "[]"
    exit 0
  fi

  # Only run if a config file is present — avoids polluting repos without ESLint
  ESLINT_CONFIG_FOUND=false
  for cfg in eslint.config.js eslint.config.mjs eslint.config.cjs \
             .eslintrc.js .eslintrc.cjs .eslintrc.yaml .eslintrc.yml .eslintrc.json .eslintrc; do
    [[ -f "$cfg" ]] && { ESLINT_CONFIG_FOUND=true; break; }
  done
  if [[ "$ESLINT_CONFIG_FOUND" == "false" ]]; then
    echo "[]"
    exit 0
  fi
fi

# Filter to JS/TS files only
JS_FILES=()
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  case "$file" in
    *.js|*.jsx|*.ts|*.tsx|*.mjs|*.cjs)
      [[ -f "$file" ]] && JS_FILES+=("$file") ;;
  esac
done <<< "$CHANGED_FILES"

if [[ ${#JS_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

if [[ -n "${ESLINT_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$ESLINT_MOCK_FILE" ]]; then
    echo "WARNING: ESLINT_MOCK_FILE '${ESLINT_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  ESLINT_OUTPUT=$(cat "$ESLINT_MOCK_FILE")
else
  # --format json; --no-warn-ignored prevents noise on paths the config ignores;
  # || true because eslint exits 1 when findings are present
  # shellcheck disable=SC2086
  ESLINT_OUTPUT=$($ESLINT_BIN --format json --no-warn-ignored "${JS_FILES[@]}" 2>/dev/null) || true
fi

if [[ -z "$ESLINT_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

# ESLint JSON structure:
# [
#   {
#     "filePath": "/abs/path/file.ts",
#     "messages": [
#       {"ruleId":"no-unused-vars","severity":2,"message":"...","line":5,"column":3}
#     ]
#   }
# ]
# severity: 2 = error → High, 1 = warning → Medium
FINDINGS=$(echo "$ESLINT_OUTPUT" | jq -r '
  [
    .[]? |
    . as $file |
    .messages[]? |
    select(.ruleId != null) |
    {
      severity: (if .severity == 2 then "High" else "Medium" end),
      confidence: 90,
      source: "eslint",
      file: $file.filePath,
      line: (.line // 1),
      finding: ("\(.ruleId): \(.message)"),
      remediation: (
        if .ruleId then
          "See https://eslint.org/docs/rules/\(.ruleId)"
        else
          "Fix ESLint violation on this line"
        end
      )
    }
  ]
' 2>/dev/null) || {
  echo "WARNING: eslint output could not be parsed; eslint findings skipped." >&2
  echo "[]"
  exit 0
}

echo "${FINDINGS:-[]}"
