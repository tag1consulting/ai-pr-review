#!/usr/bin/env bash
#
# run-semgrep.sh — Run semgrep on changed files and emit findings.
#
# Usage:
#   ./run-semgrep.sh <changed_files_list>
#
# Output:
#   JSON array of findings compatible with review.sh merge_findings.
#   Outputs "[]" if semgrep is unavailable, no files match, or no issues found.
#
# Environment:
#   SEMGREP_MOCK_FILE   When set to a readable file path, read semgrep JSON
#                       output from that file instead of running the binary.
#                       Used by the bats test suite; unset in production.

set -euo pipefail

CHANGED_FILES="${1:-}"

if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: jq not installed; semgrep check skipped." >&2
  echo "[]"
  exit 0
fi

if [[ -z "${SEMGREP_MOCK_FILE:-}" ]] && ! command -v semgrep >/dev/null 2>&1; then
  echo "WARNING: semgrep not installed; semgrep check skipped." >&2
  echo "[]"
  exit 0
fi

# Collect files that exist on disk (semgrep only scans present files)
TARGET_FILES=()
while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  [[ -f "$file" ]] && TARGET_FILES+=("$file")
done <<< "$CHANGED_FILES"

if [[ ${#TARGET_FILES[@]} -eq 0 ]]; then
  echo "[]"
  exit 0
fi

# Resolve semgrep config. Prefer the baked-in rule bundle shipped with the
# container image (no network fetch, deterministic ruleset). Fall back to
# `--config=auto` when the bundle is absent — that lets the script remain
# usable outside the container (direct script invocation, composite-action
# users who install semgrep themselves).
#
# SEMGREP_RULES_DIR can also be overridden by consumers who want to point
# at their own rule bundle.
SEMGREP_RULES_DIR="${SEMGREP_RULES_DIR:-/opt/ai-pr-review/semgrep-rules}"
SEMGREP_CONFIG_ARGS=()
if [[ -d "$SEMGREP_RULES_DIR" ]] \
   && compgen -G "$SEMGREP_RULES_DIR/*.yml" > /dev/null; then
  for rule_file in "$SEMGREP_RULES_DIR"/*.yml; do
    SEMGREP_CONFIG_ARGS+=(--config "$rule_file")
  done
else
  SEMGREP_CONFIG_ARGS+=(--config=auto)
fi

# Run semgrep (or read mock)
if [[ -n "${SEMGREP_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$SEMGREP_MOCK_FILE" ]]; then
    echo "WARNING: SEMGREP_MOCK_FILE '${SEMGREP_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  SEMGREP_OUTPUT=$(cat "$SEMGREP_MOCK_FILE")
else
  SEMGREP_STDERR=$(mktemp)
  trap 'rm -f "$SEMGREP_STDERR"' EXIT
  SEMGREP_OUTPUT=$(semgrep --json "${SEMGREP_CONFIG_ARGS[@]}" --quiet "${TARGET_FILES[@]}" 2>"$SEMGREP_STDERR") || true
  if [[ -z "$SEMGREP_OUTPUT" ]]; then
    echo "WARNING: semgrep produced no output — possible network failure or config error. semgrep stderr: $(cat "$SEMGREP_STDERR")" >&2
    echo "[]"
    exit 0
  fi
fi

if [[ -z "$SEMGREP_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

# Convert semgrep JSON results to the findings schema
FINDINGS=$(echo "$SEMGREP_OUTPUT" | jq -r '
  [
    .results[]? |
    {
      severity: (
        if .extra.severity == "ERROR"   then "High"
        elif .extra.severity == "WARNING" then "Medium"
        else "Low"
        end
      ),
      confidence: 90,
      source: "semgrep",
      file: .path,
      line: .start.line,
      finding: ("\(.check_id): \(.extra.message)"),
      remediation: (
        if .extra.metadata.references[0] then
          "See \(.extra.metadata.references[0])"
        else
          "Review the semgrep rule: \(.check_id)"
        end
      )
    }
  ]
' 2>/dev/null) || {
  echo "WARNING: semgrep output could not be parsed; semgrep findings skipped." >&2
  echo "[]"
  exit 0
}

echo "${FINDINGS:-[]}"
