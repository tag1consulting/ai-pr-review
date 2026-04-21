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
# golangci-lint does not accept individual file paths; derive unique package
# directories from the changed Go files and pass them as ./dir/... patterns.
if [[ -n "${GOLANGCI_MOCK_FILE:-}" ]]; then
  if [[ ! -r "$GOLANGCI_MOCK_FILE" ]]; then
    echo "WARNING: GOLANGCI_MOCK_FILE '${GOLANGCI_MOCK_FILE}' is not readable." >&2
    echo "[]"
    exit 0
  fi
  GL_OUTPUT=$(cat "$GOLANGCI_MOCK_FILE")
else
  # golangci-lint must run from the Go module root (where go.mod lives).
  # Walk up from the first changed file's directory to find go.mod.
  MODULE_ROOT=""
  _dir=$(dirname "${GO_FILES[0]}")
  while [[ "$_dir" != "/" && "$_dir" != "." ]]; do
    if [[ -f "$_dir/go.mod" ]]; then
      MODULE_ROOT="$_dir"
      break
    fi
    _dir=$(dirname "$_dir")
  done
  # Also check CWD itself
  if [[ -z "$MODULE_ROOT" && -f "go.mod" ]]; then
    MODULE_ROOT="."
  fi
  if [[ -z "$MODULE_ROOT" ]]; then
    echo "WARNING: could not find go.mod — golangci-lint check skipped." >&2
    echo "[]"
    exit 0
  fi

  # Derive unique package directories relative to the module root
  PKG_PATTERNS=()
  declare -A _seen_dirs
  for f in "${GO_FILES[@]}"; do
    d=$(dirname "$f")
    # Make relative to module root
    rel_d="${d#"${MODULE_ROOT}/"}"
    [[ "$rel_d" == "$d" ]] && rel_d="."  # file is in module root itself
    if [[ -z "${_seen_dirs[$rel_d]+x}" ]]; then
      _seen_dirs[$rel_d]=1
      PKG_PATTERNS+=("./${rel_d}/...")
    fi
  done

  GOLANGCI_STDERR=$(mktemp)
  GL_OUTPUT=$(cd "$MODULE_ROOT" && golangci-lint run --out-format=json --issues-exit-code=0 "${PKG_PATTERNS[@]}" 2>"$GOLANGCI_STDERR") || true
  if [[ -z "$GL_OUTPUT" ]]; then
    echo "WARNING: golangci-lint produced no output. stderr: $(cat "$GOLANGCI_STDERR")" >&2
    rm -f "$GOLANGCI_STDERR"
    echo "[]"
    exit 0
  fi
  rm -f "$GOLANGCI_STDERR"
fi

if [[ -z "$GL_OUTPUT" ]]; then
  echo "[]"
  exit 0
fi

# golangci-lint reports Pos.Filename relative to the module root.
# Prepend MODULE_ROOT (if set and non-trivial) so the path matches git-relative paths.
FILE_PREFIX=""
if [[ -n "${MODULE_ROOT:-}" && "$MODULE_ROOT" != "." ]]; then
  FILE_PREFIX="${MODULE_ROOT}/"
fi

# Convert golangci-lint JSON to the findings schema.
# Severity mapping by linter name:
#   errcheck, govet, staticcheck → High
#   everything else               → Medium
FINDINGS=$(echo "$GL_OUTPUT" | jq -r --arg prefix "$FILE_PREFIX" '
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
      file: ($prefix + .Pos.Filename),
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
