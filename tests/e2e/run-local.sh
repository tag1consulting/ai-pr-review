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
# --show-token is required: glab auth status masks the token by default
# (prints "Token: gl***" or similar), so the previous grep against its
# plain output always captured the masked placeholder, not a usable
# credential -- every local GitLab leg failed auth with that value.
: "${E2E_GITLAB_TOKEN:=$(glab auth status --hostname gitlab.com --show-token 2>&1 | grep -oP 'Token: \K\S+' || true)}"
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

# Build a candidate image from THIS checkout rather than defaulting to the
# published :dev tag. The whole point of this harness is validating this
# checkout's own code; running against :dev would silently validate
# whatever was last published instead, with no signal that the local
# uncommitted/unpushed changes were never actually exercised (the same
# class of gap the release e2e.yml workflow avoids by building fresh from
# the PR's own code -- see e2e.yml's "build" job, which this mirrors).
: "${AI_PR_REVIEW_E2E_IMAGE:=ai-pr-review:e2e-local}"
export AI_PR_REVIEW_E2E_IMAGE
echo "run-local.sh: building ${AI_PR_REVIEW_E2E_IMAGE} from ${REPO_ROOT} ..." >&2
docker build -t "${AI_PR_REVIEW_E2E_IMAGE}" "${REPO_ROOT}"

exec python -m tests.e2e.run_e2e run "$@"
