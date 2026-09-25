#!/usr/bin/env bash
# Local wrapper for the deterministic e2e harness: maps a developer's local
# gh/glab/Bitbucket app-password credentials plus a local Anthropic test key
# into the E2E_* env vars tests/e2e/run_e2e.py expects, then runs the CLI.
#
# COST WARNING: this invokes `run`, which opens real throwaway PRs/MRs and
# makes real, billed LLM API calls. Do not run this without having already
# confirmed that with a human, per this repo's CLAUDE.md checkpoint
# conventions (see tests/e2e/README.md).
set -euo pipefail

# Resolve to the checkout this script itself lives in, not a hardcoded path.
# This specifically fixes a known bug (project memory:
# feedback_e2e_workflow_repopath_not_branch.md) where a prior e2e workflow
# silently ran against the main checkout's current HEAD instead of the
# worktree/branch it was invoked from. cd'ing to the script's own directory
# tree guarantees `python -m tests.e2e.run_e2e` resolves ai_pr_review/ from
# THIS checkout, whichever worktree that happens to be.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd)"
cd "${REPO_ROOT}"

: "${E2E_GITHUB_TOKEN:=$(gh auth token 2>/dev/null || true)}"
: "${E2E_GITLAB_TOKEN:=$(glab auth status --hostname gitlab.com 2>&1 | grep -oP 'Token: \K\S+' || true)}"
export E2E_GITHUB_TOKEN
export E2E_GITLAB_TOKEN
export E2E_GITHUB_REVIEWER_TOKEN="${E2E_GITHUB_REVIEWER_TOKEN:-${E2E_GITHUB_TOKEN}}"
export E2E_BITBUCKET_EMAIL="${E2E_BITBUCKET_EMAIL:-}"
export E2E_BITBUCKET_TOKEN="${E2E_BITBUCKET_TOKEN:-}"
export E2E_ANTHROPIC_API_KEY="${E2E_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"

if [[ -z "${E2E_GITHUB_TOKEN}" ]]; then
  echo "run-local.sh: E2E_GITHUB_TOKEN not set and 'gh auth token' returned nothing; set it explicitly." >&2
  exit 2
fi

echo "run-local.sh: running from ${REPO_ROOT} (branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown))" >&2
echo "run-local.sh: this makes real, billed API calls against real test platforms." >&2

exec python -m tests.e2e.run_e2e run "$@"
