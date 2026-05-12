#!/usr/bin/env bash
# tests/golden/lint_secrets.sh — scan fixture tapes for leaked secrets.
#
# Fails with exit code 1 if any known secret pattern is found in the fixture
# corpus under tests/golden/fixtures/. Run in CI on every PR.
#
# Patterns checked:
#   ghp_*, ghs_*, gho_*, ghr_*, github_pat_*  — GitHub tokens
#   glpat-*, glcbt-*                           — GitLab PATs/CI tokens
#   sk-*, sk-proj-*                            — OpenAI API keys
#   AKIA[A-Z0-9]{16}                           — AWS access key IDs
#   Bearer <long-token>                        — OAuth2 bearer tokens in bodies
#
# Usage:
#   tests/golden/lint_secrets.sh [fixture_dir]
#   Default fixture_dir: tests/golden/fixtures

set -euo pipefail

FIXTURE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures}"

if [[ ! -d "$FIXTURE_DIR" ]]; then
  echo "INFO: No fixtures directory at ${FIXTURE_DIR}; skipping secret scan."
  exit 0
fi

PATTERNS=(
  '(ghp|ghs|gho|ghr|github_pat)_[A-Za-z0-9_]{20,}'
  'glpat-[A-Za-z0-9_-]{20,}'
  'glcbt-[A-Za-z0-9_-]{20,}'
  '(sk-|sk-proj-)[A-Za-z0-9_-]{20,}'
  'AKIA[A-Z0-9]{16}'
  'Bearer [A-Za-z0-9_.~+/-]{30,}'
)

found=0
for pattern in "${PATTERNS[@]}"; do
  while IFS= read -r match; do
    echo "SECRET LEAK: ${match}"
    found=1
  done < <(grep -rEn "$pattern" "$FIXTURE_DIR" 2>/dev/null || true)
done

if [[ "$found" -ne 0 ]]; then
  echo "ERROR: Secret patterns detected in fixture corpus. Redact before committing." >&2
  exit 1
fi

echo "OK: No secret patterns found in ${FIXTURE_DIR}"
