#!/usr/bin/env bash
#
# review.sh — AI PR Review orchestrator.
#
# Computes the diff, builds a file manifest, detects languages, calls
# review agents via the configured LLM provider, assembles the results,
# and posts them to the PR.
#
# Environment (required):
#   AI_PROVIDER       — LLM provider: anthropic | openai | openai-compatible | google | bedrock-proxy
#   GH_TOKEN          — GitHub token for posting reviews
#   PR_NUMBER         — Pull request number
#   BASE_REF          — Base branch name (e.g., main)
#   HEAD_SHA          — Head commit SHA
#   GITHUB_REPOSITORY — owner/repo
#
#   Provider credentials (one set required based on AI_PROVIDER):
#     anthropic:          ANTHROPIC_API_KEY
#     openai:             OPENAI_API_KEY
#     openai-compatible:  OPENAI_API_KEY, OPENAI_BASE_URL
#     google:             GOOGLE_API_KEY
#     bedrock-proxy:      BEDROCK_API_URL, BEDROCK_API_KEY
#
# Environment (optional):
#   AI_REVIEW_MODE    — "quick" (default) or "full"
#                       Add the "ai-review-full" label to a PR for full mode.
#   FORCE_FULL_DIFF   — "true" to bypass SHA watermark and force full-PR diff.
#                       Prefer the "ai-review-rescan" PR label instead (env-var only).
#   REVIEW_TARGET     — "pr" (default) or "standalone"
#                       "standalone" skips SHA watermark, posts findings as a GitHub issue.
#   AI_MODEL_STANDARD — Model for standard agents (pr-summarizer, code-reviewer, etc.)
#                       Defaults are chosen per provider if not set.
#   AI_MODEL_PREMIUM  — Model for deep agents (architecture-reviewer, security-reviewer)
#                       Defaults to AI_MODEL_STANDARD if not set.
#   AI_TEMPERATURE    — Sampling temperature (default: 0.3)
#   AI_DRY_RUN        — "true" to skip posting and print findings to stdout instead.
#                       Useful for local iteration; no GitHub token write access needed.
#   VCS_PROVIDER      — "github" (default) or "bitbucket". Selects the post-review
#                       script. Bitbucket support requires BITBUCKET_EMAIL and
#                       BITBUCKET_API_TOKEN for auth; standalone mode is GitHub-only.


# log_error emits ::error:: on GitHub Actions (where it renders in the UI) and
# a plain ERROR: prefix elsewhere (Bitbucket Pipelines, local runs) so the log
# annotation directives don't appear as literal noise outside GitHub Actions.
log_error() { [[ "${VCS_PROVIDER:-github}" == "github" ]] && echo "::error::$*" >&2 || echo "ERROR: $*" >&2; }
log_warn()  { [[ "${VCS_PROVIDER:-github}" == "github" ]] && echo "::warning::$*" >&2 || echo "WARNING: $*" >&2; }

# Initialized at file scope so cleanup() is safe to invoke before main() sets
# these (e.g. via a signal trap installed by a test harness, or if the EOF
# guard is bypassed). main() re-initializes them after its own trap install.
TMPFILES=()
EFFECTIVE_PROMPT_PREFIX=""

cleanup() {
  rm -f "${TMPFILES[@]}" 2>/dev/null || true
  if [[ -n "${EFFECTIVE_PROMPT_PREFIX}" ]]; then
    # Quote the prefix so spaces/metacharacters don't word-split; the trailing
    # -*.md is unquoted on purpose so the shell expands the glob.
    # shellcheck disable=SC2086 # intentional glob expansion of -*.md suffix
    rm -f "${EFFECTIVE_PROMPT_PREFIX}"-*.md 2>/dev/null || true
  fi
}


mktemp_tracked() {
  local f
  f=$(mktemp "$@")
  TMPFILES+=("$f")
  echo "$f"
}

# --- Fetch PR/MR title and description from the VCS provider ---
# Outputs: sets PR_TITLE and PR_BODY globals. Both default to empty string
# on failure (non-fatal — reviews proceed without PR context).
# Truncates PR_BODY to 4000 chars to bound token cost.
fetch_pr_description() {
  local pr_number="$1"
  local vcs_provider="$2"
  local max_body_chars=4000

  PR_TITLE=""
  PR_BODY=""

  case "$vcs_provider" in
    github)
      local owner repo pr_json
      if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
        echo "WARNING: GITHUB_REPOSITORY is not set; cannot fetch PR description." >&2
        return 0
      fi
      owner="${GITHUB_REPOSITORY%%/*}"
      repo="${GITHUB_REPOSITORY##*/}"
      pr_json=$(gh api "repos/${owner}/${repo}/pulls/${pr_number}" 2>/dev/null) || {
        echo "WARNING: Could not fetch PR description from GitHub API; proceeding without it." >&2
        return 0
      }
      PR_TITLE=$(printf '%s' "$pr_json" | jq -r '.title // ""' 2>/dev/null) || true
      PR_BODY=$(printf '%s' "$pr_json" | jq -r '.body // ""' 2>/dev/null) || true
      ;;
    gitlab)
      local gl_api gl_auth gl_project_id mr_json
      gl_api="${GITLAB_API_URL:-https://gitlab.com/api/v4}"
      gl_auth=""
      if [[ -n "${GITLAB_TOKEN:-}" ]]; then
        if [[ "$GITLAB_TOKEN" == glpat-* ]]; then
          gl_auth="PRIVATE-TOKEN: ${GITLAB_TOKEN}"
        elif [[ "$GITLAB_TOKEN" == glcbt-* ]]; then
          gl_auth="JOB-TOKEN: ${GITLAB_TOKEN}"
        else
          gl_auth="Authorization: Bearer ${GITLAB_TOKEN}"
        fi
      elif [[ -n "${CI_JOB_TOKEN:-}" ]]; then
        gl_auth="JOB-TOKEN: ${CI_JOB_TOKEN}"
      fi
      if [[ -z "$gl_auth" ]]; then
        echo "WARNING: No GitLab token available; cannot fetch MR description." >&2
        return 0
      fi
      gl_project_id="${GITLAB_PROJECT_ID:-${CI_PROJECT_ID:-}}"
      if [[ -z "$gl_project_id" ]]; then
        gl_project_id=$(printf '%s' "${CI_PROJECT_PATH:-${GITHUB_REPOSITORY:-}}" | sed 's|/|%2F|g')
      fi
      if [[ -z "$gl_project_id" ]]; then
        echo "WARNING: Cannot determine GitLab project ID; skipping MR description fetch." >&2
        return 0
      fi
      mr_json=$(curl -sS -H "$gl_auth" \
        "${gl_api}/projects/${gl_project_id}/merge_requests/${pr_number}" 2>/dev/null) || {
        echo "WARNING: Could not fetch MR description from GitLab API; proceeding without it." >&2
        return 0
      }
      PR_TITLE=$(echo "$mr_json" | jq -r '.title // ""' 2>/dev/null) || true
      PR_BODY=$(echo "$mr_json" | jq -r '.description // ""' 2>/dev/null) || true
      ;;
    bitbucket)
      local bb_workspace bb_repo_slug bb_json
      if [[ -n "${BITBUCKET_WORKSPACE:-}" && -n "${BITBUCKET_REPO_SLUG:-}" ]]; then
        bb_workspace="$BITBUCKET_WORKSPACE"
        bb_repo_slug="$BITBUCKET_REPO_SLUG"
      elif [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
        bb_workspace="${GITHUB_REPOSITORY%%/*}"
        bb_repo_slug="${GITHUB_REPOSITORY##*/}"
      else
        echo "WARNING: Cannot determine Bitbucket workspace/repo; skipping PR description fetch." >&2
        return 0
      fi
      if [[ -z "${BITBUCKET_API_TOKEN:-}" ]]; then
        echo "WARNING: BITBUCKET_API_TOKEN is not set; cannot fetch PR description from Bitbucket." >&2
        return 0
      fi
      local bb_auth
      bb_auth=$(printf '%s:%s' "${BITBUCKET_EMAIL:-}" "$BITBUCKET_API_TOKEN" | base64 | tr -d '\n')
      bb_json=$(curl -sS -H "Authorization: Basic ${bb_auth}" \
        "https://api.bitbucket.org/2.0/repositories/${bb_workspace}/${bb_repo_slug}/pullrequests/${pr_number}" 2>/dev/null) || {
        echo "WARNING: Could not fetch PR description from Bitbucket API; proceeding without it." >&2
        return 0
      }
      PR_TITLE=$(echo "$bb_json" | jq -r '.title // ""' 2>/dev/null) || true
      PR_BODY=$(echo "$bb_json" | jq -r '.description // ""' 2>/dev/null) || true
      ;;
    *)
      echo "WARNING: Unknown VCS provider '${vcs_provider}' for PR description fetch." >&2
      return 0
      ;;
  esac

  # Strip PR template boilerplate before truncation so agents get meaningful content
  if [[ -n "$PR_BODY" ]]; then
    local stripped
    stripped=$(printf '%s\n' "$PR_BODY" | sed '/^<!--.*-->/d')
    if [[ -n "$stripped" ]]; then
      PR_BODY="$stripped"
    fi
  fi

  # Truncate body to bound token cost
  if [[ ${#PR_BODY} -gt $max_body_chars ]]; then
    PR_BODY="${PR_BODY:0:$max_body_chars}…[truncated]"
  fi
}


# warn_epic3_capability_misconfig — surface Epic 3 capability/engine mismatches.
#
# The three Epic 3 capabilities (context enrichment, SARIF ingestion, learning
# loop) are implemented only in the Python engine.  When they are enabled but
# the engine is "bash", they silently do nothing — the operator who set the
# flag has no way to know.  We emit a loud GitHub-Actions-style ::warning::
# (also visible on Bitbucket / GitLab as a plain stderr line) so the
# misconfiguration is visible in the run log.
#
# Warns rather than fail-fasts to match the codebase's fail-soft posture
# (e.g. missing tree-sitter grammar → warn + regex fallback).
#
# Arguments:
#   $1 — the resolved AI_PR_REVIEW_ENGINE value
# Reads env vars:
#   AI_CONTEXT_ENRICHMENT, AI_FEEDBACK_LOOP — bool flags
#   AI_SARIF_PATHS — non-empty string triggers the warning
warn_epic3_capability_misconfig() {
  local engine="$1"
  if [[ "$engine" == "python" ]]; then
    return 0
  fi
  # Hoist all locals to the top so that under `set -e`, a `local` declaration
  # at the start of a loop iteration cannot mask an assignment failure on its
  # RHS — `local` itself always exits 0.
  local var val val_lc feature
  for pair in \
      "AI_CONTEXT_ENRICHMENT:tree-sitter context enrichment (Capability A)" \
      "AI_FEEDBACK_LOOP:learning loop (Capability C)"; do
    var="${pair%%:*}"
    feature="${pair#*:}"
    val="${!var:-}"
    # Lowercase for parity with ai_pr_review/config.py _bool(), which accepts
    # true / 1 / yes case-insensitively. Without this, AI_CONTEXT_ENRICHMENT=yes
    # on the bash engine is silently dropped with no warning.
    val_lc="${val,,}"
    if [[ "$val_lc" == "true" || "$val_lc" == "1" || "$val_lc" == "yes" ]]; then
      echo "::warning::${var}=${val} but AI_PR_REVIEW_ENGINE=${engine}; ${feature} requires the Python engine and will be IGNORED. Set AI_PR_REVIEW_ENGINE=python to enable." >&2
    fi
  done
  if [[ -n "${AI_SARIF_PATHS:-}" ]]; then
    echo "::warning::AI_SARIF_PATHS is set but AI_PR_REVIEW_ENGINE=${engine}; SARIF ingestion (Capability B) requires the Python engine and will be IGNORED. Set AI_PR_REVIEW_ENGINE=python to enable." >&2
  fi
}


# Emit a deprecation warning when the legacy bash engine is explicitly selected.
# As of v1.0.0 the default engine is "python"; this path is reached only when
# AI_PR_REVIEW_ENGINE=bash (or an unknown value) is set explicitly. The bash
# pipeline is scheduled for removal in Epic 5.
# Fail-soft: always returns 0 and never blocks the review.
# Defined as a top-level function so it is testable via
# tests/warn_bash_engine_deprecated.bats.
# Arguments: $1 — the resolved AI_PR_REVIEW_ENGINE value
warn_bash_engine_deprecated() {
  local engine="$1"
  echo "::warning::AI_PR_REVIEW_ENGINE=${engine} selects the legacy bash engine, which is DEPRECATED as of v1.0.0. The Python engine is now the default and the bash pipeline will be removed in a future major release. Set AI_PR_REVIEW_ENGINE=python (or unset it) to use the supported engine." >&2
}


main() {

  set -euo pipefail

  # Mask provider API keys in GitHub Actions logs (defense-in-depth; also covers
  # direct invocations outside action.yml). Keep in sync with the env: mapping in
  # action.yml (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY / BEDROCK_API_KEY).
  for key_var in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY BEDROCK_API_KEY GH_TOKEN BITBUCKET_API_TOKEN GITLAB_TOKEN; do
    if [[ -n "${!key_var:-}" ]]; then
      echo "::add-mask::${!key_var}"
    fi
  done

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  export AI_PR_REVIEW_SCRIPT_DIR="$SCRIPT_DIR"
  REVIEW_MODE="${AI_REVIEW_MODE:-quick}"
  REVIEW_TARGET="${REVIEW_TARGET:-pr}"
  PR_NUMBER="${PR_NUMBER:-}"
  VCS_PROVIDER="${VCS_PROVIDER:-github}"

  # Source library modules. Sourced inside main() because SCRIPT_DIR isn't
  # available at file scope. Bats tests source lib/<name>.sh directly when
  # they need these helpers — main() is not called in test context (see the
  # EOF guard).
  # shellcheck source=lib/pricing.sh
  [[ -f "${SCRIPT_DIR}/lib/pricing.sh" ]] || { log_error "lib/pricing.sh not found at ${SCRIPT_DIR}/lib/pricing.sh; check Docker image build (COPY lib/)."; exit 1; }
  source "${SCRIPT_DIR}/lib/pricing.sh"
  # shellcheck source=lib/languages.sh
  [[ -f "${SCRIPT_DIR}/lib/languages.sh" ]] || { log_error "lib/languages.sh not found at ${SCRIPT_DIR}/lib/languages.sh; check Docker image build (COPY lib/)."; exit 1; }
  source "${SCRIPT_DIR}/lib/languages.sh"
  # shellcheck source=lib/findings.sh
  [[ -f "${SCRIPT_DIR}/lib/findings.sh" ]] || { log_error "lib/findings.sh not found at ${SCRIPT_DIR}/lib/findings.sh; check Docker image build (COPY lib/)."; exit 1; }
  source "${SCRIPT_DIR}/lib/findings.sh"
  # shellcheck source=lib/agents.sh
  [[ -f "${SCRIPT_DIR}/lib/agents.sh" ]] || { log_error "lib/agents.sh not found at ${SCRIPT_DIR}/lib/agents.sh; check Docker image build (COPY lib/)."; exit 1; }
  source "${SCRIPT_DIR}/lib/agents.sh"
  # shellcheck source=lib/diff.sh
  [[ -f "${SCRIPT_DIR}/lib/diff.sh" ]] || { log_error "lib/diff.sh not found at ${SCRIPT_DIR}/lib/diff.sh; check Docker image build (COPY lib/)."; exit 1; }
  source "${SCRIPT_DIR}/lib/diff.sh"

  # Resolve the provider-specific post-review script. GitHub uses the canonical
  # post-review.sh; other providers use sibling scripts (e.g. post-review-bitbucket.sh).
  case "$VCS_PROVIDER" in
    github)    POST_REVIEW_SCRIPT="${SCRIPT_DIR}/post-review.sh" ;;
    bitbucket) POST_REVIEW_SCRIPT="${SCRIPT_DIR}/post-review-bitbucket.sh" ;;
    gitlab)    POST_REVIEW_SCRIPT="${SCRIPT_DIR}/post-review-gitlab.sh" ;;
    *)
      log_error "Invalid VCS_PROVIDER '${VCS_PROVIDER}'. Valid values: github, bitbucket, gitlab"
      exit 1
      ;;
  esac

  if [[ ! -x "$POST_REVIEW_SCRIPT" ]]; then
    log_error "Post-review script not found or not executable: ${POST_REVIEW_SCRIPT}"
    exit 1
  fi

  if [[ "$VCS_PROVIDER" == "bitbucket" && "$REVIEW_TARGET" == "standalone" ]]; then
    log_error "Standalone review mode is not supported for VCS_PROVIDER=bitbucket."
    echo "Bitbucket Cloud has no Issues product; use REVIEW_TARGET=pr instead." >&2
    exit 1
  fi

  : "${AI_PROVIDER:?AI_PROVIDER is required (anthropic|openai|openai-compatible|google|bedrock-proxy)}"

  # Validate provider early — fail fast before expensive diff computation.
  case "$AI_PROVIDER" in
    anthropic|openai|openai-compatible|google|bedrock-proxy) ;;
    *)
      log_error "Invalid AI_PROVIDER '${AI_PROVIDER}'. Valid values: anthropic, openai, openai-compatible, google, bedrock-proxy"
      exit 1
      ;;
  esac

  # Set per-provider model defaults; user env vars take precedence.
  case "$AI_PROVIDER" in
    anthropic)
      AI_MODEL_STANDARD="${AI_MODEL_STANDARD:-claude-sonnet-4-6}"
      AI_MODEL_PREMIUM="${AI_MODEL_PREMIUM:-claude-opus-4-7}"
      ;;
    openai)
      AI_MODEL_STANDARD="${AI_MODEL_STANDARD:-gpt-5.4-mini}"
      AI_MODEL_PREMIUM="${AI_MODEL_PREMIUM:-gpt-5.4}"
      ;;
    openai-compatible)
      # No universal default — user must specify a model for custom endpoints.
      AI_MODEL_STANDARD="${AI_MODEL_STANDARD:?AI_MODEL_STANDARD is required for AI_PROVIDER=openai-compatible}"
      AI_MODEL_PREMIUM="${AI_MODEL_PREMIUM:-${AI_MODEL_STANDARD}}"
      ;;
    google)
      AI_MODEL_STANDARD="${AI_MODEL_STANDARD:-gemini-2.5-flash}"
      AI_MODEL_PREMIUM="${AI_MODEL_PREMIUM:-gemini-2.5-pro}"
      ;;
    bedrock-proxy)
      AI_MODEL_STANDARD="${AI_MODEL_STANDARD:-us.anthropic.claude-sonnet-4-6}"
      AI_MODEL_PREMIUM="${AI_MODEL_PREMIUM:-global.anthropic.claude-opus-4-7}"
      ;;
  esac

  # Temp files — cleaned up on exit
  TMPFILES=()
  # Prefix used by effective_prompt() for its combined-prompt files. These are
  # created in $(...) subshells where TMPFILES+= cannot mutate the parent, so we
  # clean them up by glob match instead of array entry. The $$ suffix scopes the
  # pattern to this run so parallel reviews don't delete each other's files.
  EFFECTIVE_PROMPT_PREFIX="/tmp/ai-review-prompt-$$"
  trap cleanup EXIT

  # ---------------------------------------------------------------------------
  # Engine dispatch — AI_PR_REVIEW_ENGINE=python runs the end-to-end Python
  # pipeline (compute + dispatch + post via the VCS provider) in a single
  # process. Default engine is "python" as of v1.0.0 (Epic 4 S9); "bash" is
  # deprecated and will be removed in Epic 5.
  # The legacy JSON-tempfile handoff (Epic 1 shim) is removed in Epic 2 S12.
  # ---------------------------------------------------------------------------
  AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-python}"

  # Surface Epic 3 capability/engine misconfigurations to stderr before
  # dispatching to either engine.  Defined as a top-level function so it's
  # testable via tests/warn_epic3_capability_misconfig.bats.
  warn_epic3_capability_misconfig "$AI_PR_REVIEW_ENGINE"

  if [[ "$AI_PR_REVIEW_ENGINE" == "python" ]]; then
    echo "Engine: python (Epic 2 mode — compute + dispatch + post via Python)" >&2
    # Export model defaults so the Python subprocess inherits them. Bash
    # sets these with := but does not auto-export bash-local assignments.
    export AI_MODEL_STANDARD AI_MODEL_PREMIUM
    if ! python3 -m ai_pr_review review; then
      log_error "Python review engine failed."
      exit 1
    fi
    exit 0
  fi

  # Warn that the bash engine is deprecated as of v1.0.0. Fail-soft — never
  # blocks the review. Defined as a top-level function for testability.
  warn_bash_engine_deprecated "$AI_PR_REVIEW_ENGINE"

  # ---------------------------------------------------------------------------
  # Phase 0: Pre-flight — compute diff, build manifest
  # ---------------------------------------------------------------------------
  if [[ "$REVIEW_TARGET" != "standalone" ]]; then
    : "${PR_NUMBER:?PR_NUMBER is required for pr review-target}"
  fi

  # Fetch PR/MR title and description for agent context
  PR_TITLE=""
  PR_BODY=""
  if [[ "$REVIEW_TARGET" != "standalone" && -n "$PR_NUMBER" ]]; then
    fetch_pr_description "$PR_NUMBER" "$VCS_PROVIDER"
  fi

  echo "=== AI PR Review ===" >&2
  if [[ "$REVIEW_TARGET" == "standalone" ]]; then
    echo "Standalone review | Base: ${BASE_REF} | Head: ${HEAD_SHA}" >&2
  else
    echo "PR: #${PR_NUMBER} | Base: ${BASE_REF} | Head: ${HEAD_SHA}" >&2
    if [[ -n "$PR_TITLE" ]]; then
      echo "Title: ${PR_TITLE}" >&2
    fi
  fi
  echo "Mode: ${REVIEW_MODE}" >&2

  # Ensure we have the base branch for diffing
  git fetch origin "${BASE_REF}" --depth=50 2>/dev/null || {
    echo "WARNING: git fetch failed; attempting to proceed with existing local refs." >&2
    if ! git rev-parse --verify "origin/${BASE_REF}" > /dev/null 2>&1; then
      echo "ERROR: origin/${BASE_REF} is not reachable. Cannot compute diff. Aborting." >&2
      exit 1
    fi
  }

  # ---------------------------------------------------------------------------
  # Incremental diff: only review commits since the last review run.
  # Fall back to the full PR diff on first run or if the last SHA is unreachable.
  # ---------------------------------------------------------------------------
  LAST_REVIEWED_SHA=""
  DIFF_BASE=""
  DIFF_LABEL=""
  DIFF_TWO_DOT=false

  if [[ "$REVIEW_TARGET" != "standalone" && "${FORCE_FULL_DIFF:-false}" != "true" ]]; then
    LAST_REVIEWED_SHA=$("$POST_REVIEW_SCRIPT" --get-last-sha "$PR_NUMBER") || {
      echo "WARNING: Could not retrieve last-reviewed SHA; falling back to full PR diff." >&2
      LAST_REVIEWED_SHA=""
    }
  elif [[ "${FORCE_FULL_DIFF:-false}" == "true" ]]; then
    echo "FORCE_FULL_DIFF=true — bypassing SHA watermark; reviewing full PR diff." >&2
  fi

  if [[ -n "$LAST_REVIEWED_SHA" && "$LAST_REVIEWED_SHA" != "$HEAD_SHA" ]]; then
    # Verify the SHA is reachable AND is an ancestor of HEAD (guards against force-push/rebase)
    if git cat-file -e "${LAST_REVIEWED_SHA}^{commit}" 2>/dev/null && \
       git merge-base --is-ancestor "$LAST_REVIEWED_SHA" "$HEAD_SHA" 2>/dev/null; then
      DIFF_BASE="$LAST_REVIEWED_SHA"
      DIFF_LABEL="incremental (${LAST_REVIEWED_SHA:0:7}..${HEAD_SHA:0:7})"
      echo "Incremental review: diffing ${LAST_REVIEWED_SHA:0:7}..${HEAD_SHA:0:7}" >&2
    else
      echo "Last-reviewed SHA ${LAST_REVIEWED_SHA:0:7} not reachable or not an ancestor; falling back to full PR diff." >&2
    fi
  fi

  if [[ -z "$DIFF_BASE" ]]; then
    # In standalone mode, if the base ref resolves to the same commit as HEAD
    # (e.g. reviewing the tip of main against itself):
    #   - If STANDALONE_DEPTH is set, diff the last N commits.
    #   - Otherwise, diff against the empty tree to review all file content.
    if [[ "$REVIEW_TARGET" == "standalone" ]]; then
      STANDALONE_DEPTH="${STANDALONE_DEPTH:-}"
      BASE_RESOLVED=$(git rev-parse "origin/${BASE_REF}" 2>/dev/null || true)
      if [[ "$BASE_RESOLVED" == "$HEAD_SHA" ]]; then
        if [[ -n "$STANDALONE_DEPTH" ]]; then
          if ! [[ "$STANDALONE_DEPTH" =~ ^[0-9]+$ ]] || [[ "$STANDALONE_DEPTH" -eq 0 ]]; then
            echo "WARNING: STANDALONE_DEPTH '${STANDALONE_DEPTH}' is not a valid positive integer; ignoring." >&2
            STANDALONE_DEPTH=""
          fi
        fi
        if [[ -n "$STANDALONE_DEPTH" ]]; then
          DIFF_BASE="HEAD~${STANDALONE_DEPTH}"
          DIFF_LABEL="standalone (last ${STANDALONE_DEPTH} commits)"
          echo "Standalone review: diffing last ${STANDALONE_DEPTH} commits (HEAD~${STANDALONE_DEPTH}...HEAD)" >&2
        else
          DIFF_BASE="$(git hash-object -t tree /dev/null)"
          DIFF_TWO_DOT=true   # empty tree is not a commit; three-dot syntax won't work
          DIFF_LABEL="standalone (full tree)"
          echo "Standalone review: diffing entire tree against empty tree (${DIFF_BASE:0:7}..${HEAD_SHA:0:7})" >&2
        fi
      fi
    fi
    if [[ -z "$DIFF_BASE" ]]; then
      DIFF_LABEL="full diff (${BASE_REF}...${HEAD_SHA:0:7})"
      echo "Full review: diffing origin/${BASE_REF}...${HEAD_SHA}" >&2
    fi
  fi

  # Compute the diff
  DIFF_FILE=$(mktemp_tracked /tmp/ai-review-diff-XXXXXXXX.txt)
  EXCL=(':!*lock.json' ':!*lock.yaml' ':!vendor/*' ':!*.sum' ':!node_modules/*')
  if [[ -n "$DIFF_BASE" ]]; then
    local_diff_sep="..."
    [[ "$DIFF_TWO_DOT" == "true" ]] && local_diff_sep=".."
    if ! git diff "${DIFF_BASE}${local_diff_sep}${HEAD_SHA}" -- "${EXCL[@]}" > "$DIFF_FILE" 2>/dev/null; then
      : > "$DIFF_FILE"
      echo "WARNING: git diff failed; diff output may be empty or incomplete." >&2
    fi
  else
    if ! git diff "origin/${BASE_REF}...${HEAD_SHA}" -- "${EXCL[@]}" > "$DIFF_FILE" 2>/dev/null; then
      : > "$DIFF_FILE"
      echo "WARNING: git diff failed; diff output may be empty or incomplete." >&2
    fi
  fi

  # Optionally filter out upstream base-branch merges from the diff.
  # Only applies to PR reviews (not standalone) with a resolved DIFF_BASE.
  AI_MERGE_FILTER_FALLBACK_REASON=""
  if [[ "${AI_IGNORE_MERGE_COMMITS:-false}" == "true" && \
        "$REVIEW_TARGET" != "standalone" && \
        -n "$DIFF_BASE" ]]; then
    if ! compute_filtered_diff "$DIFF_BASE" "$HEAD_SHA"; then
      echo "::warning::Merge-commit filtering failed (${AI_MERGE_FILTER_FALLBACK_REASON}); using unfiltered diff." >&2
    fi
  fi

  # Check for empty diff
  DIFF_LINES=$(wc -l < "$DIFF_FILE" | tr -d ' ')
  if [[ "$DIFF_LINES" -eq 0 ]]; then
    echo "No new changes since last review. Skipping." >&2
    exit 0
  fi

  # Enforce diff size hard cap to prevent runaway token consumption.
  # Configurable via MAX_DIFF_LINES env var (default: 5000).
  MAX_DIFF_LINES="${MAX_DIFF_LINES:-5000}"
  if ! [[ "$MAX_DIFF_LINES" =~ ^[0-9]+$ ]]; then
    echo "WARNING: MAX_DIFF_LINES '${MAX_DIFF_LINES}' is not a valid integer; using default 5000." >&2
    MAX_DIFF_LINES=5000
  fi
  if [[ "$DIFF_LINES" -gt "$MAX_DIFF_LINES" ]]; then
    log_warn "Diff is too large (${DIFF_LINES} lines; limit ${MAX_DIFF_LINES}). Skipping AI review."
    echo "To review large diffs, increase MAX_DIFF_LINES or split into smaller changes." >&2
    if [[ "$REVIEW_TARGET" != "standalone" ]]; then
      post_skip_comment "$DIFF_LINES" "$MAX_DIFF_LINES"
    fi
    exit 0
  fi

  echo "Diff: ${DIFF_LINES} lines (${DIFF_LABEL})" >&2

  # Build file manifest (same range as diff).
  # CHANGED_FILES and DIFF_STAT are consumed by build_file_manifest() in lib/diff.sh.
  # shellcheck disable=SC2034
  if [[ -n "$DIFF_BASE" ]]; then
    CHANGED_FILES=$(git diff --name-only -z "${DIFF_BASE}${local_diff_sep}${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tr '\0' '\n' || true)
    DIFF_STAT=$(git diff --stat "${DIFF_BASE}${local_diff_sep}${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tail -1)
  else
    CHANGED_FILES=$(git diff --name-only -z "origin/${BASE_REF}...${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tr '\0' '\n' || true)
    DIFF_STAT=$(git diff --stat "origin/${BASE_REF}...${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tail -1)
  fi

  build_file_manifest || exit 0

  # Validate and log review mode
  if [[ "$REVIEW_MODE" != "quick" && "$REVIEW_MODE" != "full" ]]; then
    echo "WARNING: Unknown AI_REVIEW_MODE '${REVIEW_MODE}'. Defaulting to quick." >&2
    REVIEW_MODE="quick"
  fi

  # ---------------------------------------------------------------------------
  # Phase 1: Prepare agent messages and call agents
  # ---------------------------------------------------------------------------
  echo "--- Calling agents ---" >&2

  # --- Build shared message files ---

  # Full context message: manifest + PR description + commit log + project context + language context + diff
  FULL_CONTEXT_MSG=$(mktemp_tracked /tmp/ai-review-full-ctx-XXXXXXXX.md)
  {
    echo "## File Manifest"
    printf '%s\n' "$MANIFEST"
    echo ""
    if [[ -n "$PR_TITLE" || -n "$PR_BODY" ]]; then
      echo "## PR Description"
      if [[ -n "$PR_TITLE" ]]; then
        echo "Title: ${PR_TITLE}"
      fi
      if [[ -n "$PR_BODY" ]]; then
        echo ""
        echo "$PR_BODY"
      fi
      echo ""
    fi
    echo "## Commit Log"
    echo "$COMMIT_LOG"
    echo ""
    if [[ -n "$PROJECT_CONTEXT" ]]; then
      echo "## Project Context"
      echo "$PROJECT_CONTEXT"
      echo ""
    fi
    if [[ -n "$LANGUAGE_CONTEXT" ]]; then
      printf '%s\n' "$LANGUAGE_CONTEXT"
      echo ""
    fi
    echo "## Review Metadata"
    echo "Current date: $(date -u +%Y-%m-%d)"
    echo "Note: Your training data has a knowledge cutoff. Software versions, GitHub Actions, APIs, and packages released after that cutoff exist but may be unfamiliar to you. Do not flag dependency versions as nonexistent or unreleased solely because you have not encountered them before."
    echo ""
    echo "## Diff"
    cat "$DIFF_FILE"
  } > "$FULL_CONTEXT_MSG"

  # Code context message: manifest + PR description + language context + diff (no commit log/project context)
  CODE_CONTEXT_MSG=$(mktemp_tracked /tmp/ai-review-code-ctx-XXXXXXXX.md)
  {
    echo "## File Manifest"
    printf '%s\n' "$MANIFEST"
    echo ""
    if [[ -n "$PR_TITLE" || -n "$PR_BODY" ]]; then
      echo "## PR Description"
      if [[ -n "$PR_TITLE" ]]; then
        echo "Title: ${PR_TITLE}"
      fi
      if [[ -n "$PR_BODY" ]]; then
        echo ""
        echo "$PR_BODY"
      fi
      echo ""
    fi
    if [[ -n "$LANGUAGE_CONTEXT" ]]; then
      printf '%s\n' "$LANGUAGE_CONTEXT"
      echo ""
    fi
    echo "## Review Metadata"
    echo "Current date: $(date -u +%Y-%m-%d)"
    echo "Note: Your training data has a knowledge cutoff. Software versions, GitHub Actions, APIs, and packages released after that cutoff exist but may be unfamiliar to you. Do not flag dependency versions as nonexistent or unreleased solely because you have not encountered them before."
    echo ""
    echo "## Diff"
    cat "$DIFF_FILE"
  } > "$CODE_CONTEXT_MSG"

  # Blind message: ONLY the raw diff (zero context — this is intentional)
  BLIND_MSG=$(mktemp_tracked /tmp/ai-review-blind-XXXXXXXX.md)
  cat "$DIFF_FILE" > "$BLIND_MSG"

  # Track agents that fail and their token usage
  FAILED_AGENTS=()
  TOKEN_LOG=()  # entries: "agent_name input=N output=N"

  # --- Detect conditional agent triggers ---
  detect_conditional_agent_triggers

  # --- Output files ---
  SUMMARY_FILE=$(mktemp_tracked /tmp/ai-review-summary-XXXXXXXX.md)
  FINDINGS_FILE=$(mktemp_tracked /tmp/ai-review-findings-XXXXXXXX.md)

  # --- Configurable limits ---
  # Validate AI_MAX_TOKENS_PER_AGENT: must be a positive integer; clamp to [256, 65536].
  # Gemini 2.5 models consume thinking tokens from the maxOutputTokens budget,
  # leaving less room for visible output. Default to 16384 for Google to avoid
  # truncation from thinking overhead (typically 3-14x the visible output).
  _default_tokens=8192
  if [[ "$AI_PROVIDER" == "google" ]]; then
    _default_tokens=16384
  fi
  _raw_tokens="${AI_MAX_TOKENS_PER_AGENT:-$_default_tokens}"
  if [[ "$_raw_tokens" =~ ^[0-9]+$ ]] && [[ "$_raw_tokens" -ge 256 ]] && [[ "$_raw_tokens" -le 65536 ]]; then
    AI_MAX_TOKENS_PER_AGENT="$_raw_tokens"
  else
    echo "WARNING: AI_MAX_TOKENS_PER_AGENT='${_raw_tokens}' is invalid; using default ${_default_tokens}." >&2
    AI_MAX_TOKENS_PER_AGENT="$_default_tokens"
  fi
  unset _raw_tokens _default_tokens

  # --- Agent roster ---
  AGENT_OUTPUTS=()

  if [[ "${AI_PARALLEL:-true}" == "true" ]]; then
    # -------------------------------------------------------------------------
    # Parallel path: tiered fan-out to cap simultaneous LLM calls and reduce
    # provider rate-limit pressure. Tier 1 (essential) and Tier 2 (full-only)
    # run as separate batches separated by a wait barrier.
    # Static analyzers (shellcheck, CVE check) run alongside Tier 1 since they
    # are independent of agent state and complete in seconds.
    # -------------------------------------------------------------------------

    echo "Running Phase 1 in parallel mode (tiered fan-out)." >&2

    # Tier 1: build TIER1_WAIT_ARGS inline at each call site so PID/output pairs
    # are always in sync — no post-hoc array-zip that could misalign on conditional agents.
    TIER1_OUTPUTS=()
    TIER1_WAIT_ARGS=()

    # Cache-priming (issue #144): Anthropic's cache only becomes visible AFTER
    # the first response begins. When 5 agents fan out concurrently against the
    # same shared context, none of them see a warm cache — each writes its own
    # entry (5 cache writes, 0 reads). Priming runs one agent synchronously on
    # the CODE_CONTEXT_MSG cohort FIRST so the cache entry is warm by the time
    # the rest of Tier 1 + all of Tier 2 fan out.
    #
    # We reuse code-reviewer as the primer (it always runs, its output is
    # useful real work, and it uses CODE_CONTEXT_MSG — the most-shared
    # context). Docs: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    # "For concurrent requests, note that a cache entry only becomes available
    # after the first response begins. If you need cache hits for parallel
    # requests, wait for the first response before sending subsequent requests."
    #
    # Wall-clock cost: code-reviewer's latency becomes serial with the rest of
    # Tier 1 (~15-30s instead of overlapping). Gain: 4 more cache-read hits
    # per run in the CODE_CONTEXT_MSG cohort, worth roughly 20-30% of the run's
    # input token cost on the typical cold-cache path.
    #
    # No priming is needed for FULL_CONTEXT_MSG (only pr-summarizer uses it
    # in Tier 1; architecture-reviewer uses it in Tier 2, after pr-summarizer
    # has already completed via the tier barrier) or BLIND_MSG (only one
    # agent uses it — blind-hunter).
    #
    # Gated by AI_CACHE_PRIMING (default: false — opt-in tuning knob).
    # Set AI_CACHE_PRIMING=true to enable synchronous primer calls before
    # Tier 1 fan-out. Default is off because live benchmarks (#153) showed
    # no net cost win vs the opportunistic-timing baseline on Bedrock.
    # Left as an opt-in for environments where concurrent cache-visibility
    # timing fails (strict rate limits, proxy serialization, etc.).
    CACHE_PRIMING_EFFECTIVE=$(cache_priming_effective)

    # Track whether security-reviewer has been primed (full mode only).
    # If yes, the Tier 2 dispatch below will skip it to avoid double-running.
    SECURITY_REVIEWER_PRIMED="false"

    if [[ "$CACHE_PRIMING_EFFECTIVE" == "true" ]]; then
      # Why two primers: Anthropic caches per-model. One primer writes a
      # Sonnet cache entry (helps adversarial-general + silent-failure-hunter
      # when those are Sonnet); a second primer is needed for the Opus
      # CODE_CONTEXT_MSG cohort (security-reviewer, edge-case-hunter,
      # silent-failure-hunter in full mode).
      #
      # Primers run in parallel with each other (different models → no
      # mutual cache dependency) so total wall-clock ≈ max(sonnet, opus)
      # latency rather than sum(sonnet, opus). Then all other agents fan out.
      #
      # The Sonnet primer is code-reviewer (always runs, always Tier 1).
      # The Opus primer is security-reviewer (only in full mode — it's a
      # Tier 2 agent pulled forward). In quick mode only the Sonnet primer
      # fires; Opus agents don't run.

      PRIMER_OUTPUTS=()
      PRIMER_WAIT_ARGS=()

      # Sonnet primer: code-reviewer — its output is part of Tier 1 anyway,
      # so running it as the primer costs only the serialization (not a wasted call).
      echo "Cache priming: warming Sonnet CODE_CONTEXT_MSG via code-reviewer." >&2
      PRIMER_OUTPUTS+=("$FINDINGS_FILE")
      call_agent_bg "code-reviewer" "$AI_MODEL_STANDARD" \
        "$(effective_prompt code-reviewer "${SCRIPT_DIR}/prompts/code-reviewer.md")" "$CODE_CONTEXT_MSG" "$FINDINGS_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
      PRIMER_WAIT_ARGS+=($! "$FINDINGS_FILE")

      # Opus primer: security-reviewer (full mode only). Pulled forward from
      # Tier 2 so the cache entry is warm before Tier 2's parallel fan-out.
      # Gated: if security-reviewer will be skipped by its diff gate, skip priming too.
      if [[ "$REVIEW_MODE" == "full" ]] && [[ "$RUN_SECURITY_REVIEWER" == "true" ]]; then
        echo "Cache priming: warming Opus CODE_CONTEXT_MSG via security-reviewer (pulled forward from Tier 2)." >&2
        SEC_FILE=$(mktemp_tracked /tmp/ai-review-sec-XXXXXXXX.md)
        PRIMER_OUTPUTS+=("$SEC_FILE")
        call_agent_bg "security-reviewer" "$AI_MODEL_PREMIUM" \
          "$(effective_prompt security-reviewer "${SCRIPT_DIR}/prompts/security-reviewer.md")" "$CODE_CONTEXT_MSG" "$SEC_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
        PRIMER_WAIT_ARGS+=($! "$SEC_FILE")
        SECURITY_REVIEWER_PRIMED="true"
      fi

      wait_tier_pids "${PRIMER_WAIT_ARGS[@]}"
      collect_parallel_results "${PRIMER_OUTPUTS[@]}"
      # Primer outputs are real review outputs — push onto AGENT_OUTPUTS so
      # Phase 2 (findings extraction) sees them alongside other Tier 1/2 agents.
      for f in "${PRIMER_OUTPUTS[@]}"; do
        AGENT_OUTPUTS+=("$f")
      done
    fi

    # pr-summarizer: only on first review (gate evaluated in parent)
    if [[ -z "$LAST_REVIEWED_SHA" ]]; then
      TIER1_OUTPUTS+=("$SUMMARY_FILE")
      call_agent_bg "pr-summarizer" "$AI_MODEL_STANDARD" \
        "${SCRIPT_DIR}/prompts/pr-summarizer.md" "$FULL_CONTEXT_MSG" "$SUMMARY_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
      TIER1_WAIT_ARGS+=($! "$SUMMARY_FILE")
    else
      echo "Skipping pr-summarizer (incremental run — summary already posted)." >&2
    fi

    # code-reviewer: only dispatch as parallel if priming didn't already run it.
    if [[ "$CACHE_PRIMING_EFFECTIVE" != "true" ]]; then
      TIER1_OUTPUTS+=("$FINDINGS_FILE")
      call_agent_bg "code-reviewer" "$AI_MODEL_STANDARD" \
        "$(effective_prompt code-reviewer "${SCRIPT_DIR}/prompts/code-reviewer.md")" "$CODE_CONTEXT_MSG" "$FINDINGS_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
      TIER1_WAIT_ARGS+=($! "$FINDINGS_FILE")
    fi

    if [[ "$HAS_ERROR_PATTERNS" -eq 1 ]]; then
      SFH_FILE=$(mktemp_tracked /tmp/ai-review-sfh-XXXXXXXX.md)
      TIER1_OUTPUTS+=("$SFH_FILE")
      SFH_MODEL="$AI_MODEL_STANDARD"
      [[ "$REVIEW_MODE" == "full" ]] && SFH_MODEL="$AI_MODEL_PREMIUM"
      call_agent_bg "silent-failure-hunter" "$SFH_MODEL" \
        "$(effective_prompt silent-failure-hunter "${SCRIPT_DIR}/prompts/silent-failure-hunter.md")" "$CODE_CONTEXT_MSG" "$SFH_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
      TIER1_WAIT_ARGS+=($! "$SFH_FILE")
    fi

    # Static analyzers run concurrently with Tier 1 agents
    SHELLCHECK_TMPFILE=$(mktemp_tracked /tmp/ai-review-sc-XXXXXXXX.json)
    CVE_TMPFILE=$(mktemp_tracked /tmp/ai-review-cve-XXXXXXXX.json)
    SEMGREP_TMPFILE=$(mktemp_tracked /tmp/ai-review-semgrep-XXXXXXXX.json)
    TRUFFLEHOG_TMPFILE=$(mktemp_tracked /tmp/ai-review-th-XXXXXXXX.json)
    RUFF_TMPFILE=$(mktemp_tracked /tmp/ai-review-ruff-XXXXXXXX.json)
    GOLANGCI_TMPFILE=$(mktemp_tracked /tmp/ai-review-gl-XXXXXXXX.json)
    HADOLINT_TMPFILE=$(mktemp_tracked /tmp/ai-review-hadolint-XXXXXXXX.json)
    CHECKOV_TMPFILE=$(mktemp_tracked /tmp/ai-review-checkov-XXXXXXXX.json)
    PHPCS_TMPFILE=$(mktemp_tracked /tmp/ai-review-phpcs-XXXXXXXX.json)
    ESLINT_TMPFILE=$(mktemp_tracked /tmp/ai-review-eslint-XXXXXXXX.json)
    PHPSTAN_TMPFILE=$(mktemp_tracked /tmp/ai-review-phpstan-XXXXXXXX.json)
    KUBELINTER_TMPFILE=$(mktemp_tracked /tmp/ai-review-kubelinter-XXXXXXXX.json)
    TFLINT_TMPFILE=$(mktemp_tracked /tmp/ai-review-tflint-XXXXXXXX.json)
    SC_PID="" CVE_PID="" SEMGREP_PID="" TRUFFLEHOG_PID="" RUFF_PID="" GOLANGCI_PID=""
    HADOLINT_PID="" CHECKOV_PID="" PHPCS_PID="" ESLINT_PID=""
    PHPSTAN_PID="" KUBELINTER_PID="" TFLINT_PID=""

    if [[ -n "$CHANGED_FILES" ]]; then
      ( "${SCRIPT_DIR}/analyzers/run-shellcheck.sh" "$CHANGED_FILES" > "$SHELLCHECK_TMPFILE" ) &
      SC_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-cve-check.sh" "$CHANGED_FILES" > "$CVE_TMPFILE" ) &
      CVE_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-semgrep.sh" "$CHANGED_FILES" > "$SEMGREP_TMPFILE" ) &
      SEMGREP_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-trufflehog.sh" "$CHANGED_FILES" > "$TRUFFLEHOG_TMPFILE" ) &
      TRUFFLEHOG_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-ruff.sh" "$CHANGED_FILES" > "$RUFF_TMPFILE" ) &
      RUFF_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-golangci-lint.sh" "$CHANGED_FILES" > "$GOLANGCI_TMPFILE" ) &
      GOLANGCI_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-hadolint.sh" "$CHANGED_FILES" > "$HADOLINT_TMPFILE" ) &
      HADOLINT_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-checkov.sh" "$CHANGED_FILES" > "$CHECKOV_TMPFILE" ) &
      CHECKOV_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-phpcs.sh" "$CHANGED_FILES" > "$PHPCS_TMPFILE" ) &
      PHPCS_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-eslint.sh" "$CHANGED_FILES" > "$ESLINT_TMPFILE" ) &
      ESLINT_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-phpstan.sh" "$CHANGED_FILES" > "$PHPSTAN_TMPFILE" ) &
      PHPSTAN_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-kube-linter.sh" "$CHANGED_FILES" > "$KUBELINTER_TMPFILE" ) &
      KUBELINTER_PID=$!
      ( "${SCRIPT_DIR}/analyzers/run-tflint.sh" "$CHANGED_FILES" > "$TFLINT_TMPFILE" ) &
      TFLINT_PID=$!
    fi

    # Wait for Tier 1 agents
    wait_tier_pids "${TIER1_WAIT_ARGS[@]}"
    collect_parallel_results "${TIER1_OUTPUTS[@]}"
    # code-reviewer and SFH outputs go into AGENT_OUTPUTS; summary is separate
    for f in "${TIER1_OUTPUTS[@]}"; do
      [[ "$f" != "$SUMMARY_FILE" ]] && AGENT_OUTPUTS+=("$f")
    done

    # Tier 2: Full mode only
    if [[ "$REVIEW_MODE" == "full" ]]; then
      TIER2_OUTPUTS=()
      TIER2_WAIT_ARGS=()

      # TODO(#129): replace RUN_* flag checks with per-entry condition callbacks
      # when the declarative agent roster lands.
      if [[ "$RUN_ARCHITECTURE_REVIEWER" == "true" ]]; then
        ARCH_FILE=$(mktemp_tracked /tmp/ai-review-arch-XXXXXXXX.md)
        TIER2_OUTPUTS+=("$ARCH_FILE")
        call_agent_bg "architecture-reviewer" "$AI_MODEL_PREMIUM" \
          "$(effective_prompt architecture-reviewer "${SCRIPT_DIR}/prompts/architecture-reviewer.md")" "$FULL_CONTEXT_MSG" "$ARCH_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
        TIER2_WAIT_ARGS+=($! "$ARCH_FILE")
      else
        echo "Skipping architecture-reviewer (gate: no code/infra files in diff; set AI_DISABLE_GATE_ARCHITECTURE=true to override)." >&2
      fi

      # security-reviewer: skip if already primed synchronously above, or gated by diff heuristic.
      #
      # Invariant note for future maintainers: when primed, SEC_FILE is NOT
      # added to TIER2_OUTPUTS. Its output and token accounting already flow
      # through the primer block (via PRIMER_OUTPUTS → collect_parallel_results
      # → AGENT_OUTPUTS). `AGENT_OUTPUTS` remains the authoritative list of
      # all agent outputs for findings extraction — do NOT rely on
      # `TIER2_OUTPUTS` to enumerate every Tier 2 agent, because primed
      # agents bypass it intentionally to avoid double-collection of their
      # .name / .tokens / .failed sidecar files.
      if [[ "$SECURITY_REVIEWER_PRIMED" != "true" ]]; then
        if [[ "$RUN_SECURITY_REVIEWER" == "true" ]]; then
          SEC_FILE=$(mktemp_tracked /tmp/ai-review-sec-XXXXXXXX.md)
          TIER2_OUTPUTS+=("$SEC_FILE")
          call_agent_bg "security-reviewer" "$AI_MODEL_PREMIUM" \
            "$(effective_prompt security-reviewer "${SCRIPT_DIR}/prompts/security-reviewer.md")" "$CODE_CONTEXT_MSG" "$SEC_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
          TIER2_WAIT_ARGS+=($! "$SEC_FILE")
        else
          echo "Skipping security-reviewer (gate: no auth/crypto patterns or security-sensitive paths in diff; set AI_DISABLE_GATE_SECURITY=true to override)." >&2
        fi
      fi

      BLIND_FILE=$(mktemp_tracked /tmp/ai-review-blind-XXXXXXXX.md)
      TIER2_OUTPUTS+=("$BLIND_FILE")
      call_agent_bg "blind-hunter" "$AI_MODEL_STANDARD" \
        "$(effective_prompt blind-hunter "${SCRIPT_DIR}/prompts/blind-hunter.md")" "$BLIND_MSG" "$BLIND_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
      TIER2_WAIT_ARGS+=($! "$BLIND_FILE")

      if [[ "$RUN_EDGE_CASE_HUNTER" == "true" ]]; then
        EDGE_FILE=$(mktemp_tracked /tmp/ai-review-edge-XXXXXXXX.md)
        TIER2_OUTPUTS+=("$EDGE_FILE")
        call_agent_bg "edge-case-hunter" "$AI_MODEL_PREMIUM" \
          "$(effective_prompt edge-case-hunter "${SCRIPT_DIR}/prompts/edge-case-hunter.md")" "$CODE_CONTEXT_MSG" "$EDGE_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
        TIER2_WAIT_ARGS+=($! "$EDGE_FILE")
      else
        echo "Skipping edge-case-hunter (gate: no control flow in diff additions; set AI_DISABLE_GATE_EDGE_CASE=true to override)." >&2
      fi

      ADV_FILE=$(mktemp_tracked /tmp/ai-review-adv-XXXXXXXX.md)
      TIER2_OUTPUTS+=("$ADV_FILE")
      call_agent_bg "adversarial-general" "$AI_MODEL_STANDARD" \
        "$(effective_prompt adversarial-general "${SCRIPT_DIR}/prompts/adversarial-general.md")" "$CODE_CONTEXT_MSG" "$ADV_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
      TIER2_WAIT_ARGS+=($! "$ADV_FILE")

      wait_tier_pids "${TIER2_WAIT_ARGS[@]}"
      collect_parallel_results "${TIER2_OUTPUTS[@]}"
      AGENT_OUTPUTS+=("${TIER2_OUTPUTS[@]}")
    fi

    # Wait for static analyzers and collect their JSON output
    if [[ -n "$SC_PID" ]]; then
      wait "$SC_PID" || echo "WARNING: run-shellcheck.sh subshell exited non-zero; shellcheck findings may be incomplete." >&2
    fi
    if [[ -n "$CVE_PID" ]]; then
      wait "$CVE_PID" || echo "WARNING: run-cve-check.sh subshell exited non-zero; CVE findings may be incomplete." >&2
    fi
    if [[ -n "$SEMGREP_PID" ]]; then
      wait "$SEMGREP_PID" || echo "WARNING: run-semgrep.sh subshell exited non-zero; semgrep findings may be incomplete." >&2
    fi
    if [[ -n "$TRUFFLEHOG_PID" ]]; then
      wait "$TRUFFLEHOG_PID" || echo "WARNING: run-trufflehog.sh subshell exited non-zero; trufflehog findings may be incomplete." >&2
    fi
    if [[ -n "$RUFF_PID" ]]; then
      wait "$RUFF_PID" || echo "WARNING: run-ruff.sh subshell exited non-zero; ruff findings may be incomplete." >&2
    fi
    if [[ -n "$GOLANGCI_PID" ]]; then
      wait "$GOLANGCI_PID" || echo "WARNING: run-golangci-lint.sh subshell exited non-zero; golangci-lint findings may be incomplete." >&2
    fi
    if [[ -n "$HADOLINT_PID" ]]; then
      wait "$HADOLINT_PID" || echo "WARNING: run-hadolint.sh subshell exited non-zero; hadolint findings may be incomplete." >&2
    fi
    if [[ -n "$CHECKOV_PID" ]]; then
      wait "$CHECKOV_PID" || echo "WARNING: run-checkov.sh subshell exited non-zero; checkov findings may be incomplete." >&2
    fi
    if [[ -n "$PHPCS_PID" ]]; then
      wait "$PHPCS_PID" || echo "WARNING: run-phpcs.sh subshell exited non-zero; phpcs findings may be incomplete." >&2
    fi
    if [[ -n "$ESLINT_PID" ]]; then
      wait "$ESLINT_PID" || echo "WARNING: run-eslint.sh subshell exited non-zero; eslint findings may be incomplete." >&2
    fi
    if [[ -n "$PHPSTAN_PID" ]]; then
      wait "$PHPSTAN_PID" || echo "WARNING: run-phpstan.sh subshell exited non-zero; phpstan findings may be incomplete." >&2
    fi
    if [[ -n "$KUBELINTER_PID" ]]; then
      wait "$KUBELINTER_PID" || echo "WARNING: run-kube-linter.sh subshell exited non-zero; kube-linter findings may be incomplete." >&2
    fi
    if [[ -n "$TFLINT_PID" ]]; then
      wait "$TFLINT_PID" || echo "WARNING: run-tflint.sh subshell exited non-zero; tflint findings may be incomplete." >&2
    fi

    SHELLCHECK_JSON=$(cat "$SHELLCHECK_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$SHELLCHECK_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-shellcheck.sh failed; shellcheck findings will be skipped." >&2
      SHELLCHECK_JSON="[]"
    fi
    SC_COUNT=$(echo "$SHELLCHECK_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$SC_COUNT" -gt 0 ]] && echo "Shellcheck: ${SC_COUNT} findings" >&2

    CVE_JSON=$(cat "$CVE_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$CVE_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-cve-check.sh failed; CVE findings will be skipped." >&2
      CVE_JSON="[]"
    fi
    CVE_COUNT=$(echo "$CVE_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$CVE_COUNT" -gt 0 ]] && echo "CVE check: ${CVE_COUNT} findings" >&2

    SEMGREP_JSON=$(cat "$SEMGREP_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$SEMGREP_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-semgrep.sh failed; semgrep findings will be skipped." >&2
      SEMGREP_JSON="[]"
    fi
    SEMGREP_COUNT=$(echo "$SEMGREP_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$SEMGREP_COUNT" -gt 0 ]] && echo "Semgrep: ${SEMGREP_COUNT} findings" >&2

    TRUFFLEHOG_JSON=$(cat "$TRUFFLEHOG_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$TRUFFLEHOG_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-trufflehog.sh failed; trufflehog findings will be skipped." >&2
      TRUFFLEHOG_JSON="[]"
    fi
    TH_COUNT=$(echo "$TRUFFLEHOG_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$TH_COUNT" -gt 0 ]] && echo "Trufflehog: ${TH_COUNT} findings" >&2

    RUFF_JSON=$(cat "$RUFF_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$RUFF_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-ruff.sh failed; ruff findings will be skipped." >&2
      RUFF_JSON="[]"
    fi
    RUFF_COUNT=$(echo "$RUFF_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$RUFF_COUNT" -gt 0 ]] && echo "Ruff: ${RUFF_COUNT} findings" >&2

    GOLANGCI_JSON=$(cat "$GOLANGCI_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$GOLANGCI_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-golangci-lint.sh failed; golangci-lint findings will be skipped." >&2
      GOLANGCI_JSON="[]"
    fi
    GL_COUNT=$(echo "$GOLANGCI_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$GL_COUNT" -gt 0 ]] && echo "Golangci-lint: ${GL_COUNT} findings" >&2

    HADOLINT_JSON=$(cat "$HADOLINT_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$HADOLINT_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-hadolint.sh failed; hadolint findings will be skipped." >&2
      HADOLINT_JSON="[]"
    fi
    HADOLINT_COUNT=$(echo "$HADOLINT_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$HADOLINT_COUNT" -gt 0 ]] && echo "Hadolint: ${HADOLINT_COUNT} findings" >&2

    CHECKOV_JSON=$(cat "$CHECKOV_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$CHECKOV_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-checkov.sh failed; checkov findings will be skipped." >&2
      CHECKOV_JSON="[]"
    fi
    CHECKOV_COUNT=$(echo "$CHECKOV_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$CHECKOV_COUNT" -gt 0 ]] && echo "Checkov: ${CHECKOV_COUNT} findings" >&2

    PHPCS_JSON=$(cat "$PHPCS_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$PHPCS_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-phpcs.sh failed; phpcs findings will be skipped." >&2
      PHPCS_JSON="[]"
    fi
    PHPCS_COUNT=$(echo "$PHPCS_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$PHPCS_COUNT" -gt 0 ]] && echo "Phpcs: ${PHPCS_COUNT} findings" >&2

    ESLINT_JSON=$(cat "$ESLINT_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$ESLINT_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-eslint.sh failed; eslint findings will be skipped." >&2
      ESLINT_JSON="[]"
    fi
    ESLINT_COUNT=$(echo "$ESLINT_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$ESLINT_COUNT" -gt 0 ]] && echo "ESLint: ${ESLINT_COUNT} findings" >&2

    PHPSTAN_JSON=$(cat "$PHPSTAN_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$PHPSTAN_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-phpstan.sh failed; phpstan findings will be skipped." >&2
      PHPSTAN_JSON="[]"
    fi
    PHPSTAN_COUNT=$(echo "$PHPSTAN_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$PHPSTAN_COUNT" -gt 0 ]] && echo "PHPStan: ${PHPSTAN_COUNT} findings" >&2

    KUBELINTER_JSON=$(cat "$KUBELINTER_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$KUBELINTER_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-kube-linter.sh failed; kube-linter findings will be skipped." >&2
      KUBELINTER_JSON="[]"
    fi
    KUBELINTER_COUNT=$(echo "$KUBELINTER_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$KUBELINTER_COUNT" -gt 0 ]] && echo "Kube-linter: ${KUBELINTER_COUNT} findings" >&2

    TFLINT_JSON=$(cat "$TFLINT_TMPFILE" 2>/dev/null || echo "[]")
    if ! echo "$TFLINT_JSON" | jq -e '.' >/dev/null 2>&1; then
      echo "WARNING: run-tflint.sh failed; tflint findings will be skipped." >&2
      TFLINT_JSON="[]"
    fi
    TFLINT_COUNT=$(echo "$TFLINT_JSON" | jq 'length' 2>/dev/null || echo "0")
    [[ "$TFLINT_COUNT" -gt 0 ]] && echo "TFLint: ${TFLINT_COUNT} findings" >&2

  else
    # -------------------------------------------------------------------------
    # Sequential path (default): unchanged from pre-parallel behavior.
    # -------------------------------------------------------------------------

    # pr-summarizer: only on the first run. Follow-up runs leave the existing
    # summary comment untouched — it describes the full PR; re-running it on an
    # incremental diff would replace a useful whole-PR overview with a partial one.
    if [[ -z "$LAST_REVIEWED_SHA" ]]; then
      call_agent "pr-summarizer" "$AI_MODEL_STANDARD" \
        "${SCRIPT_DIR}/prompts/pr-summarizer.md" "$FULL_CONTEXT_MSG" "$SUMMARY_FILE" "$AI_MAX_TOKENS_PER_AGENT"
    else
      echo "Skipping pr-summarizer (incremental run — summary already posted)." >&2
    fi

    call_agent "code-reviewer" "$AI_MODEL_STANDARD" \
      "$(effective_prompt code-reviewer "${SCRIPT_DIR}/prompts/code-reviewer.md")" "$CODE_CONTEXT_MSG" "$FINDINGS_FILE" "$AI_MAX_TOKENS_PER_AGENT"
    AGENT_OUTPUTS+=("$FINDINGS_FILE")

    # Tier 1 conditional: run in both quick and full when triggered
    if [[ "$HAS_ERROR_PATTERNS" -eq 1 ]]; then
      SFH_FILE=$(mktemp_tracked /tmp/ai-review-sfh-XXXXXXXX.md)
      SFH_MODEL="$AI_MODEL_STANDARD"
      [[ "$REVIEW_MODE" == "full" ]] && SFH_MODEL="$AI_MODEL_PREMIUM"
      call_agent "silent-failure-hunter" "$SFH_MODEL" \
        "$(effective_prompt silent-failure-hunter "${SCRIPT_DIR}/prompts/silent-failure-hunter.md")" "$CODE_CONTEXT_MSG" "$SFH_FILE" "$AI_MAX_TOKENS_PER_AGENT"
      AGENT_OUTPUTS+=("$SFH_FILE")
    fi

    # Tier 2: Full mode only
    if [[ "$REVIEW_MODE" == "full" ]]; then
      if [[ "$RUN_ARCHITECTURE_REVIEWER" == "true" ]]; then
        ARCH_FILE=$(mktemp_tracked /tmp/ai-review-arch-XXXXXXXX.md)
        call_agent "architecture-reviewer" "$AI_MODEL_PREMIUM" \
          "$(effective_prompt architecture-reviewer "${SCRIPT_DIR}/prompts/architecture-reviewer.md")" "$FULL_CONTEXT_MSG" "$ARCH_FILE" "$AI_MAX_TOKENS_PER_AGENT"
        AGENT_OUTPUTS+=("$ARCH_FILE")
      else
        echo "Skipping architecture-reviewer (gate: no code/infra files in diff; set AI_DISABLE_GATE_ARCHITECTURE=true to override)." >&2
      fi

      if [[ "$RUN_SECURITY_REVIEWER" == "true" ]]; then
        SEC_FILE=$(mktemp_tracked /tmp/ai-review-sec-XXXXXXXX.md)
        call_agent "security-reviewer" "$AI_MODEL_PREMIUM" \
          "$(effective_prompt security-reviewer "${SCRIPT_DIR}/prompts/security-reviewer.md")" "$CODE_CONTEXT_MSG" "$SEC_FILE" "$AI_MAX_TOKENS_PER_AGENT"
        AGENT_OUTPUTS+=("$SEC_FILE")
      else
        echo "Skipping security-reviewer (gate: no auth/crypto patterns or security-sensitive paths in diff; set AI_DISABLE_GATE_SECURITY=true to override)." >&2
      fi

      BLIND_FILE=$(mktemp_tracked /tmp/ai-review-blind-XXXXXXXX.md)
      call_agent "blind-hunter" "$AI_MODEL_STANDARD" \
        "$(effective_prompt blind-hunter "${SCRIPT_DIR}/prompts/blind-hunter.md")" "$BLIND_MSG" "$BLIND_FILE" "$AI_MAX_TOKENS_PER_AGENT"
      AGENT_OUTPUTS+=("$BLIND_FILE")

      if [[ "$RUN_EDGE_CASE_HUNTER" == "true" ]]; then
        EDGE_FILE=$(mktemp_tracked /tmp/ai-review-edge-XXXXXXXX.md)
        call_agent "edge-case-hunter" "$AI_MODEL_PREMIUM" \
          "$(effective_prompt edge-case-hunter "${SCRIPT_DIR}/prompts/edge-case-hunter.md")" "$CODE_CONTEXT_MSG" "$EDGE_FILE" "$AI_MAX_TOKENS_PER_AGENT"
        AGENT_OUTPUTS+=("$EDGE_FILE")
      else
        echo "Skipping edge-case-hunter (gate: no control flow in diff additions; set AI_DISABLE_GATE_EDGE_CASE=true to override)." >&2
      fi

      ADV_FILE=$(mktemp_tracked /tmp/ai-review-adv-XXXXXXXX.md)
      call_agent "adversarial-general" "$AI_MODEL_STANDARD" \
        "$(effective_prompt adversarial-general "${SCRIPT_DIR}/prompts/adversarial-general.md")" "$CODE_CONTEXT_MSG" "$ADV_FILE" "$AI_MAX_TOKENS_PER_AGENT"
      AGENT_OUTPUTS+=("$ADV_FILE")
    fi

    # --- Run shellcheck if shell files changed ---
    SHELLCHECK_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      SHELLCHECK_JSON=$("${SCRIPT_DIR}/analyzers/run-shellcheck.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-shellcheck.sh failed; shellcheck findings will be skipped." >&2
        SHELLCHECK_JSON="[]"
      }
      SC_COUNT=$(echo "$SHELLCHECK_JSON" | jq 'length' 2>/dev/null || echo "0")
      if [[ "$SC_COUNT" -gt 0 ]]; then
        echo "Shellcheck: ${SC_COUNT} findings" >&2
      fi
    fi

    # --- Run CVE check if dependency manifests changed ---
    CVE_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      CVE_JSON=$("${SCRIPT_DIR}/analyzers/run-cve-check.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-cve-check.sh failed; CVE findings will be skipped." >&2
        CVE_JSON="[]"
      }
      CVE_COUNT=$(echo "$CVE_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$CVE_COUNT" -gt 0 ]] && echo "CVE check: ${CVE_COUNT} findings" >&2
    fi

    # --- Run semgrep ---
    SEMGREP_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      SEMGREP_JSON=$("${SCRIPT_DIR}/analyzers/run-semgrep.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-semgrep.sh failed; semgrep findings will be skipped." >&2
        SEMGREP_JSON="[]"
      }
      SEMGREP_COUNT=$(echo "$SEMGREP_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$SEMGREP_COUNT" -gt 0 ]] && echo "Semgrep: ${SEMGREP_COUNT} findings" >&2
    fi

    # --- Run trufflehog ---
    TRUFFLEHOG_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      TRUFFLEHOG_JSON=$("${SCRIPT_DIR}/analyzers/run-trufflehog.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-trufflehog.sh failed; trufflehog findings will be skipped." >&2
        TRUFFLEHOG_JSON="[]"
      }
      TH_COUNT=$(echo "$TRUFFLEHOG_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$TH_COUNT" -gt 0 ]] && echo "Trufflehog: ${TH_COUNT} findings" >&2
    fi

    # --- Run ruff (Python files only) ---
    RUFF_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      RUFF_JSON=$("${SCRIPT_DIR}/analyzers/run-ruff.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-ruff.sh failed; ruff findings will be skipped." >&2
        RUFF_JSON="[]"
      }
      RUFF_COUNT=$(echo "$RUFF_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$RUFF_COUNT" -gt 0 ]] && echo "Ruff: ${RUFF_COUNT} findings" >&2
    fi

    # --- Run golangci-lint (Go files only) ---
    GOLANGCI_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      GOLANGCI_JSON=$("${SCRIPT_DIR}/analyzers/run-golangci-lint.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-golangci-lint.sh failed; golangci-lint findings will be skipped." >&2
        GOLANGCI_JSON="[]"
      }
      GL_COUNT=$(echo "$GOLANGCI_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$GL_COUNT" -gt 0 ]] && echo "Golangci-lint: ${GL_COUNT} findings" >&2
    fi

    # --- Run hadolint (Dockerfiles only) ---
    HADOLINT_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      HADOLINT_JSON=$("${SCRIPT_DIR}/analyzers/run-hadolint.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-hadolint.sh failed; hadolint findings will be skipped." >&2
        HADOLINT_JSON="[]"
      }
      HADOLINT_COUNT=$(echo "$HADOLINT_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$HADOLINT_COUNT" -gt 0 ]] && echo "Hadolint: ${HADOLINT_COUNT} findings" >&2
    fi

    # --- Run checkov (Terraform, K8s YAML, Dockerfiles, JSON) ---
    CHECKOV_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      CHECKOV_JSON=$("${SCRIPT_DIR}/analyzers/run-checkov.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-checkov.sh failed; checkov findings will be skipped." >&2
        CHECKOV_JSON="[]"
      }
      CHECKOV_COUNT=$(echo "$CHECKOV_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$CHECKOV_COUNT" -gt 0 ]] && echo "Checkov: ${CHECKOV_COUNT} findings" >&2
    fi

    # --- Run phpcs (PHP files only) ---
    PHPCS_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      PHPCS_JSON=$("${SCRIPT_DIR}/analyzers/run-phpcs.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-phpcs.sh failed; phpcs findings will be skipped." >&2
        PHPCS_JSON="[]"
      }
      PHPCS_COUNT=$(echo "$PHPCS_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$PHPCS_COUNT" -gt 0 ]] && echo "Phpcs: ${PHPCS_COUNT} findings" >&2
    fi

    # --- Run eslint (JS/TS files only, requires consumer config) ---
    ESLINT_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      ESLINT_JSON=$("${SCRIPT_DIR}/analyzers/run-eslint.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-eslint.sh failed; eslint findings will be skipped." >&2
        ESLINT_JSON="[]"
      }
      ESLINT_COUNT=$(echo "$ESLINT_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$ESLINT_COUNT" -gt 0 ]] && echo "ESLint: ${ESLINT_COUNT} findings" >&2
    fi

    # --- Run phpstan (PHP files only) ---
    PHPSTAN_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      PHPSTAN_JSON=$("${SCRIPT_DIR}/analyzers/run-phpstan.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-phpstan.sh failed; phpstan findings will be skipped." >&2
        PHPSTAN_JSON="[]"
      }
      PHPSTAN_COUNT=$(echo "$PHPSTAN_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$PHPSTAN_COUNT" -gt 0 ]] && echo "PHPStan: ${PHPSTAN_COUNT} findings" >&2
    fi

    # --- Run kube-linter (Kubernetes YAML/JSON manifests only) ---
    KUBELINTER_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      KUBELINTER_JSON=$("${SCRIPT_DIR}/analyzers/run-kube-linter.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-kube-linter.sh failed; kube-linter findings will be skipped." >&2
        KUBELINTER_JSON="[]"
      }
      KUBELINTER_COUNT=$(echo "$KUBELINTER_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$KUBELINTER_COUNT" -gt 0 ]] && echo "Kube-linter: ${KUBELINTER_COUNT} findings" >&2
    fi

    # --- Run tflint (Terraform files only) ---
    TFLINT_JSON="[]"
    if [[ -n "$CHANGED_FILES" ]]; then
      TFLINT_JSON=$("${SCRIPT_DIR}/analyzers/run-tflint.sh" "$CHANGED_FILES") || {
        echo "WARNING: run-tflint.sh failed; tflint findings will be skipped." >&2
        TFLINT_JSON="[]"
      }
      TFLINT_COUNT=$(echo "$TFLINT_JSON" | jq 'length' 2>/dev/null || echo "0")
      [[ "$TFLINT_COUNT" -gt 0 ]] && echo "TFLint: ${TFLINT_COUNT} findings" >&2
    fi

  fi  # end AI_PARALLEL branch

  AGENT_COUNT=${#AGENT_OUTPUTS[@]}
  FAILED_COUNT=${#FAILED_AGENTS[@]}
  if [[ "$FAILED_COUNT" -gt 0 ]]; then
    echo "Agents complete. (${AGENT_COUNT} finding agents ran, ${FAILED_COUNT} failed: ${FAILED_AGENTS[*]})" >&2
    if [[ "$FAILED_COUNT" -ge "$AGENT_COUNT" && "$AGENT_COUNT" -gt 0 ]]; then
      echo "ERROR: All ${AGENT_COUNT} agents failed. Aborting review." >&2
      exit 1
    fi
  else
    echo "Agents complete. (${AGENT_COUNT} finding agents ran)" >&2
  fi

  # Log token usage summary
  if [[ "${#TOKEN_LOG[@]}" -gt 0 ]]; then
    echo "--- Token usage ---" >&2
    # Flag suggestion-enabled runs so operators can attribute token spend to
    # this feature in Actions logs (each eligible agent carries ~400 extra
    # input tokens for the suggestion addendum, plus variable output tokens).
    _enable_flag="${AI_ENABLE_SUGGESTIONS:-true}"
    if [[ "${_enable_flag,,}" == "true" ]]; then
      echo "  Suggestions: enabled" >&2
    fi
    TOTAL_INPUT=0
    TOTAL_OUTPUT=0
    for entry in "${TOKEN_LOG[@]}"; do
      echo "  ${entry}" >&2
      in_tok=$(echo "$entry" | grep -oE '(^| )input=[0-9]+' | sed 's/.*input=//' || echo "0")
      out_tok=$(echo "$entry" | grep -oE '(^| )output=[0-9]+' | sed 's/.*output=//' || echo "0")
      TOTAL_INPUT=$(( TOTAL_INPUT + in_tok ))
      TOTAL_OUTPUT=$(( TOTAL_OUTPUT + out_tok ))
    done
    echo "  TOTAL: input=${TOTAL_INPUT} output=${TOTAL_OUTPUT} (combined=$(( TOTAL_INPUT + TOTAL_OUTPUT )))" >&2
  fi

  # ---------------------------------------------------------------------------
  # Phase 2: Parse and merge findings JSON from all agents
  # ---------------------------------------------------------------------------
  FINDINGS_JSON_FILE=$(mktemp_tracked /tmp/ai-review-findings-json-XXXXXXXX.json)
  echo "[]" > "$FINDINGS_JSON_FILE"

  for agent_output in "${AGENT_OUTPUTS[@]}"; do
    # .name sidecar is written by both call_agent and call_agent_bg
    agent_name_for_output=$(cat "${agent_output}.name" 2>/dev/null || echo "unknown")
    AGENT_JSON=$(extract_findings "$agent_output" "$agent_name_for_output")
    if [[ "$AGENT_JSON" != "[]" ]]; then
      merge_findings "$AGENT_JSON"
    fi
  done

  # Merge shellcheck findings
  if [[ "$SHELLCHECK_JSON" != "[]" ]]; then
    merge_findings "$SHELLCHECK_JSON"
  fi

  # Merge CVE findings
  if [[ "$CVE_JSON" != "[]" ]]; then
    merge_findings "$CVE_JSON"
  fi

  # Merge semgrep findings
  if [[ "$SEMGREP_JSON" != "[]" ]]; then
    merge_findings "$SEMGREP_JSON"
  fi

  # Merge trufflehog findings
  if [[ "$TRUFFLEHOG_JSON" != "[]" ]]; then
    merge_findings "$TRUFFLEHOG_JSON"
  fi

  # Merge ruff findings
  if [[ "$RUFF_JSON" != "[]" ]]; then
    merge_findings "$RUFF_JSON"
  fi

  # Merge golangci-lint findings
  if [[ "$GOLANGCI_JSON" != "[]" ]]; then
    merge_findings "$GOLANGCI_JSON"
  fi

  # Merge hadolint findings
  if [[ "$HADOLINT_JSON" != "[]" ]]; then
    merge_findings "$HADOLINT_JSON"
  fi

  # Merge checkov findings
  if [[ "$CHECKOV_JSON" != "[]" ]]; then
    merge_findings "$CHECKOV_JSON"
  fi

  # Merge phpcs findings
  if [[ "$PHPCS_JSON" != "[]" ]]; then
    merge_findings "$PHPCS_JSON"
  fi

  # Merge eslint findings
  if [[ "$ESLINT_JSON" != "[]" ]]; then
    merge_findings "$ESLINT_JSON"
  fi

  # Merge phpstan findings
  if [[ "$PHPSTAN_JSON" != "[]" ]]; then
    merge_findings "$PHPSTAN_JSON"
  fi

  # Merge kube-linter findings
  if [[ "$KUBELINTER_JSON" != "[]" ]]; then
    merge_findings "$KUBELINTER_JSON"
  fi

  # Merge tflint findings
  if [[ "$TFLINT_JSON" != "[]" ]]; then
    merge_findings "$TFLINT_JSON"
  fi

  # Filter out findings below confidence threshold BEFORE suppressions so that
  # verify-gated suppression rules don't fire registry HTTP calls on sub-threshold noise.
  _raw_conf="${AI_CONFIDENCE_THRESHOLD:-75}"
  if [[ "$_raw_conf" =~ ^[0-9]+$ ]] && [[ "$_raw_conf" -ge 0 ]] && [[ "$_raw_conf" -le 100 ]]; then
    CONFIDENCE_THRESHOLD="$_raw_conf"
  else
    echo "WARNING: AI_CONFIDENCE_THRESHOLD='${_raw_conf}' is invalid; using default 75." >&2
    CONFIDENCE_THRESHOLD=75
  fi
  unset _raw_conf

  PRE_FILTER_COUNT=$(jq 'length' "$FINDINGS_JSON_FILE" 2>/dev/null || echo "0")
  if jq --argjson t "$CONFIDENCE_THRESHOLD" '[.[] | select((.confidence // 0) >= $t)]' \
      "$FINDINGS_JSON_FILE" > "${FINDINGS_JSON_FILE}.tmp"; then
    mv "${FINDINGS_JSON_FILE}.tmp" "$FINDINGS_JSON_FILE"
  else
    echo "WARNING: Confidence filter jq failed; keeping all findings unfiltered." >&2
    rm -f "${FINDINGS_JSON_FILE}.tmp"
  fi
  POST_FILTER_COUNT=$(jq 'length' "$FINDINGS_JSON_FILE" 2>/dev/null || echo "0")
  if [[ "$PRE_FILTER_COUNT" -ne "$POST_FILTER_COUNT" ]]; then
    echo "Filtered findings: ${PRE_FILTER_COUNT} → ${POST_FILTER_COUNT} (confidence >= ${CONFIDENCE_THRESHOLD})" >&2
  fi

  # Apply declarative suppressions (won't-fix / false positives) after confidence filter
  SUPPRESSED_COUNT=0
  apply_suppressions

  # Deduplicate findings: merge findings within 3 lines of the cluster *start* in the
  # same file. Clusters are closed when a new finding is more than 3 lines from the
  # first item in the current cluster (not from the previous item), preventing
  # single-linkage drift where the cluster stretches unboundedly. The highest severity
  # finding in each cluster is kept.
  if jq '
    def sev_rank: if . == "Critical" then 4 elif . == "High" then 3
      elif . == "Medium" then 2 else 1 end;
    # Merge two cluster states: carry best finding and accumulate all sources.
    # cur.best.source is already in cur.sources; only f.source is new each call.
    def merge_cluster(cur; f):
      if (cur.best.severity | sev_rank) >= (f.severity | sev_rank)
      then {start: cur.start, best: cur.best, sources: (cur.sources + [f.source // "unknown"])}
      else {start: cur.start, best: f, sources: (cur.sources + [f.source // "unknown"])}
      end;
    group_by(.file // "unknown")
    | map(
        sort_by(.line // 0) |
        reduce .[] as $f (
          {clusters: [], cur: null};
          if .cur == null then
            {clusters: .clusters, cur: {start: ($f.line // 0), best: $f, sources: [$f.source // "unknown"]}}
          elif (($f.line // 0) - .cur.start) <= 3
          then
            # Still within 3 lines of cluster start: keep higher-severity, accumulate sources
            {clusters: .clusters, cur: (merge_cluster(.cur; $f))}
          else
            # Beyond 3 lines of cluster start: close current cluster, open new one
            {clusters: (.clusters + [.cur.best + {sources: (.cur.sources | unique | sort)}]),
             cur: {start: ($f.line // 0), best: $f, sources: [$f.source // "unknown"]}}
          end
        )
        | if .cur != null
          then .clusters + [.cur.best + {sources: (.cur.sources | unique | sort)}]
          else .clusters end
      )
    | flatten
  ' "$FINDINGS_JSON_FILE" > "${FINDINGS_JSON_FILE}.tmp"; then
    mv "${FINDINGS_JSON_FILE}.tmp" "$FINDINGS_JSON_FILE"
  else
    echo "WARNING: Dedup jq failed; findings may contain duplicates." >&2
    rm -f "${FINDINGS_JSON_FILE}.tmp"
  fi
  DEDUP_COUNT=$(jq 'length' "$FINDINGS_JSON_FILE" 2>/dev/null || echo "0")
  echo "Total findings after dedup: ${DEDUP_COUNT}" >&2

  # Build merged findings markdown from all agent outputs (strip json-findings blocks)
  FINDINGS_CLEAN_FILE=$(mktemp_tracked /tmp/ai-review-findings-clean-XXXXXXXX.md)
  : > "$FINDINGS_CLEAN_FILE"
  for agent_output in "${AGENT_OUTPUTS[@]}"; do
    # Single quotes are intentional — the fenced marker is a literal sed pattern,
    # not a shell expansion.
    # shellcheck disable=SC2016
    AGENT_CONTENT=$(sed '/```json-findings/,/```/d' "$agent_output")
    if [[ -n "$AGENT_CONTENT" ]]; then
      echo "$AGENT_CONTENT" >> "$FINDINGS_CLEAN_FILE"
      echo "" >> "$FINDINGS_CLEAN_FILE"
    fi
  done

  # Append failed agent notice if any agents failed
  if [[ "${#FAILED_AGENTS[@]}" -gt 0 ]]; then
    echo "> **Note:** The following agents failed and their output is excluded: ${FAILED_AGENTS[*]}" >> "$FINDINGS_CLEAN_FILE"
  fi

  # ---------------------------------------------------------------------------
  # Pricing and display name lookups — backed by model-pricing.json.
  # Rates are in microdollars per million tokens (integer, no floating point).
  # All rates are public list prices; update model-pricing.json when prices change.
  # ---------------------------------------------------------------------------
  # Returns two integers: IN_RATE OUT_RATE (microdollars per million tokens).
  # Callers compute: cost_microdollars = tokens * rate / 1_000_000
  # shellcheck disable=SC2034 # consumed by model_pricing/model_display_name in lib/pricing.sh (sourced above)
  MODEL_PRICING_FILE="${SCRIPT_DIR}/config/model-pricing.json"

  # Build token usage table as a collapsed details block
  TOKEN_TABLE_FILE=$(mktemp_tracked /tmp/ai-review-token-table-XXXXXXXX.md)
  if [[ "${#TOKEN_LOG[@]}" -gt 0 ]]; then
    {
      echo "<details>"
      echo "<summary>Token usage by agent</summary>"
      echo ""
      emit_token_table
      echo ""
      echo "_Prices are public list rates and do not reflect discounts, commitments, or proxy markups._"
      echo ""
      echo "</details>"
    } > "$TOKEN_TABLE_FILE"
  fi

  # ---------------------------------------------------------------------------
  # Phase 3: Post to GitHub
  # ---------------------------------------------------------------------------

  if [[ "${AI_DRY_RUN:-false}" == "true" ]]; then
    echo "--- AI_DRY_RUN=true: skipping GitHub post, printing findings ---" >&2
    echo "=== Summary ===" && cat "$SUMMARY_FILE"
    echo "=== Findings ===" && cat "$FINDINGS_CLEAN_FILE"
    echo "=== AI PR Review complete (dry run) ===" >&2
    exit 0
  fi

  echo "--- Posting review (provider: ${VCS_PROVIDER}) ---" >&2

  # Pass failed agents to post-review.sh so it can avoid a false APPROVE.
  # Use colon as delimiter (agent names never contain colons).
  if [[ "${#FAILED_AGENTS[@]}" -gt 0 ]]; then
    export AI_REVIEW_FAILED_AGENTS
    AI_REVIEW_FAILED_AGENTS=$(IFS=:; echo "${FAILED_AGENTS[*]}")
  fi

  if [[ "$REVIEW_TARGET" == "standalone" ]]; then
    "$POST_REVIEW_SCRIPT" \
      --standalone \
      "$SUMMARY_FILE" \
      "$FINDINGS_CLEAN_FILE" \
      "$FINDINGS_JSON_FILE" \
      "$DIFF_FILE" \
      "$HEAD_SHA" \
      "$TOKEN_TABLE_FILE"
  else
    "$POST_REVIEW_SCRIPT" \
      "$PR_NUMBER" \
      "$SUMMARY_FILE" \
      "$FINDINGS_CLEAN_FILE" \
      "$FINDINGS_JSON_FILE" \
      "$DIFF_FILE" \
      "$HEAD_SHA" \
      "$TOKEN_TABLE_FILE"
  fi

  # ---------------------------------------------------------------------------
  # Phase 4: Summary to step summary
  # ---------------------------------------------------------------------------
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo "## AI PR Review Results"
      echo ""
      echo "**Mode:** ${REVIEW_MODE} | **Diff:** ${DIFF_LABEL}"
      echo "**Files:** ${FILE_COUNT}"
      echo "**Languages:** ${LANGUAGES:-none detected}"
      echo "**Agents:** ${AGENT_COUNT} finding agents"
      if [[ "${#FAILED_AGENTS[@]}" -gt 0 ]]; then
        echo "**Failed agents:** ${FAILED_AGENTS[*]}"
      fi
      echo ""
      FINDING_COUNT=$(jq 'length' "$FINDINGS_JSON_FILE" 2>/dev/null || echo "0")
      if [[ "${SUPPRESSED_COUNT:-0}" -gt 0 ]]; then
        echo "**Findings:** ${FINDING_COUNT} (${SUPPRESSED_COUNT} suppressed)"
      else
        echo "**Findings:** ${FINDING_COUNT}"
      fi
      echo ""
      if [[ "${#TOKEN_LOG[@]}" -gt 0 ]]; then
        echo "### Token Usage"
        echo ""
        emit_token_table
        echo ""
        echo "_Prices are public list rates and do not reflect discounts, commitments, or proxy markups._"
        echo ""
      fi
      echo "### Summary"
      cat "$SUMMARY_FILE"
    } >> "$GITHUB_STEP_SUMMARY"
  fi

  echo "=== AI PR Review complete ===" >&2
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
