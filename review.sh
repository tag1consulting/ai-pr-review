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
#                       Add the "ai-review-rescan" label to a PR to set this.
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

set -euo pipefail

# Mask provider API keys in GitHub Actions logs (defense-in-depth; also covers
# direct invocations outside action.yml). Keep in sync with the env: mapping in
# action.yml (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY / BEDROCK_API_KEY).
for key_var in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY BEDROCK_API_KEY GH_TOKEN BITBUCKET_API_TOKEN; do
  if [[ -n "${!key_var:-}" ]]; then
    echo "::add-mask::${!key_var}"
  fi
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVIEW_MODE="${AI_REVIEW_MODE:-quick}"
REVIEW_TARGET="${REVIEW_TARGET:-pr}"
PR_NUMBER="${PR_NUMBER:-}"
VCS_PROVIDER="${VCS_PROVIDER:-github}"

# log_error emits ::error:: on GitHub Actions (where it renders in the UI) and
# a plain ERROR: prefix elsewhere (Bitbucket Pipelines, local runs) so the log
# annotation directives don't appear as literal noise outside GitHub Actions.
log_error() { [[ "${VCS_PROVIDER:-github}" == "github" ]] && echo "::error::$*" >&2 || echo "ERROR: $*" >&2; }
log_warn()  { [[ "${VCS_PROVIDER:-github}" == "github" ]] && echo "::warning::$*" >&2 || echo "WARNING: $*" >&2; }

# Resolve the provider-specific post-review script. GitHub uses the canonical
# post-review.sh; other providers use sibling scripts (e.g. post-review-bitbucket.sh).
case "$VCS_PROVIDER" in
  github)    POST_REVIEW_SCRIPT="${SCRIPT_DIR}/post-review.sh" ;;
  bitbucket) POST_REVIEW_SCRIPT="${SCRIPT_DIR}/post-review-bitbucket.sh" ;;
  *)
    log_error "Invalid VCS_PROVIDER '${VCS_PROVIDER}'. Valid values: github, bitbucket"
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
    AI_MODEL_STANDARD="${AI_MODEL_STANDARD:-gpt-4o}"
    AI_MODEL_PREMIUM="${AI_MODEL_PREMIUM:-${AI_MODEL_STANDARD}}"
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
cleanup() {
  rm -f "${TMPFILES[@]}" 2>/dev/null || true
  # shellcheck disable=SC2086 # intentional glob expansion
  rm -f ${EFFECTIVE_PROMPT_PREFIX}-*.md 2>/dev/null || true
}
trap cleanup EXIT

mktemp_tracked() {
  local f
  f=$(mktemp "$@")
  TMPFILES+=("$f")
  echo "$f"
}

# ---------------------------------------------------------------------------
# Phase 0: Pre-flight — compute diff, build manifest
# ---------------------------------------------------------------------------
if [[ "$REVIEW_TARGET" != "standalone" ]]; then
  : "${PR_NUMBER:?PR_NUMBER is required for pr review-target}"
fi

echo "=== AI PR Review ===" >&2
if [[ "$REVIEW_TARGET" == "standalone" ]]; then
  echo "Standalone review | Base: ${BASE_REF} | Head: ${HEAD_SHA}" >&2
else
  echo "PR: #${PR_NUMBER} | Base: ${BASE_REF} | Head: ${HEAD_SHA}" >&2
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
  if [[ "$REVIEW_TARGET" != "standalone" && "$VCS_PROVIDER" == "github" ]]; then
  # Post (or update) a comment explaining the skip — idempotent via marker to avoid
  # accumulating duplicate comments across repeated oversized pushes. GitHub-only
  # in v0.2.0; Bitbucket consumers will see the log_warn output but
  # no comment will be posted (the review still exits cleanly).
  : "${GH_TOKEN:?GH_TOKEN is required}"
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
  : "${PR_NUMBER:?PR_NUMBER is required}"
  OWNER="${GITHUB_REPOSITORY%%/*}"
  REPO="${GITHUB_REPOSITORY##*/}"
  SKIP_MARKER="<!-- ai-pr-review-skipped -->"
  SKIP_BODY="${SKIP_MARKER}
## AI Review Skipped

This PR's diff is too large for automated review (${DIFF_LINES} lines; limit: ${MAX_DIFF_LINES}).

To review anyway, increase \`MAX_DIFF_LINES\` in the workflow or split this PR into smaller changes."
  existing_skip_id=$(gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
    --paginate \
    --jq ".[] | select(.body | contains(\"${SKIP_MARKER}\")) | .id" \
    2>/dev/null | tail -1) || true
  if [[ -n "$existing_skip_id" ]]; then
    gh api "repos/${OWNER}/${REPO}/issues/comments/${existing_skip_id}" \
      --method PATCH --field body="$SKIP_BODY" > /dev/null 2>&1 || true
  else
    gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
      --method POST --field body="$SKIP_BODY" > /dev/null 2>&1 || true
  fi
  fi  # end REVIEW_TARGET != standalone && VCS_PROVIDER == github
  exit 0
fi

echo "Diff: ${DIFF_LINES} lines (${DIFF_LABEL})" >&2

# Build file manifest (same range as diff)
if [[ -n "$DIFF_BASE" ]]; then
  CHANGED_FILES=$(git diff --name-only -z "${DIFF_BASE}${local_diff_sep}${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tr '\0' '\n' || true)
  DIFF_STAT=$(git diff --stat "${DIFF_BASE}${local_diff_sep}${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tail -1)
else
  CHANGED_FILES=$(git diff --name-only -z "origin/${BASE_REF}...${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tr '\0' '\n' || true)
  DIFF_STAT=$(git diff --stat "origin/${BASE_REF}...${HEAD_SHA}" -- "${EXCL[@]}" 2>/dev/null | tail -1)
fi

if [[ -z "$CHANGED_FILES" ]]; then
  echo "No changed files after exclusions. Skipping review." >&2
  exit 0
fi
FILE_COUNT=$(echo "$CHANGED_FILES" | wc -l | tr -d ' ')

# Detect languages from extensions
LANGUAGES=""
detect_language() {
  local ext="$1"
  case "$ext" in
    go) echo "Go" ;;
    py) echo "Python" ;;
    js|jsx) echo "JavaScript" ;;
    ts|tsx) echo "TypeScript" ;;
    php|module|theme|inc) echo "PHP" ;;
    tf|tfvars) echo "Terraform" ;;
    sh|bash) echo "Shell" ;;
    yaml|yml) echo "YAML" ;;
    rb|rake|gemspec) echo "Ruby" ;;
    rs) echo "Rust" ;;
    java) echo "Java" ;;
    c|h|cpp|hpp|cc|cxx) echo "C++" ;;
    *) echo "" ;;
  esac
}

is_test_file() {
  local file="$1"
  [[ "$file" =~ _test\.go$ ]] ||
  [[ "$file" =~ test_.*\.py$ ]] ||
  [[ "$file" =~ _test\.py$ ]] ||
  [[ "$file" =~ \.test\.[jt]sx?$ ]] ||
  [[ "$file" =~ \.spec\.[jt]sx?$ ]] ||
  [[ "$file" =~ _spec\.rb$ ]] ||
  [[ "$file" =~ _test\.rb$ ]] ||
  [[ "$file" =~ Test\.java$ ]] ||
  [[ "$file" =~ TestBase\.php$ ]] ||
  [[ "$file" =~ Test\.php$ ]] ||
  [[ "$file" =~ _test\.cpp$ ]] ||
  [[ "$file" =~ _test\.cc$ ]] ||
  [[ "$file" =~ _test\.ts$ ]] ||
  [[ "$file" =~ /tests/ ]] ||
  [[ "$file" =~ /test/ ]] ||
  [[ "$file" =~ /spec/ ]]
}

DETECTED_LANGS=()
while IFS= read -r file; do
  ext="${file##*.}"
  lang=$(detect_language "$ext")
  if [[ -n "$lang" ]]; then
    # Add to array if not already present
    found=0
    for existing in "${DETECTED_LANGS[@]+"${DETECTED_LANGS[@]}"}"; do
      if [[ "$existing" == "$lang" ]]; then
        found=1
        break
      fi
    done
    if [[ "$found" -eq 0 ]]; then
      DETECTED_LANGS+=("$lang")
    fi
  fi
done <<< "$CHANGED_FILES"

LANGUAGES=$(IFS=", "; echo "${DETECTED_LANGS[*]+"${DETECTED_LANGS[*]}"}")
echo "Languages: ${LANGUAGES:-none detected}" >&2
echo "Files: ${FILE_COUNT} | ${DIFF_STAT}" >&2

# Categorize files
SOURCE_FILES=""
TEST_FILES=""
CONFIG_FILES=""
DOC_FILES=""
while IFS= read -r file; do
  if is_test_file "$file"; then
    TEST_FILES="${TEST_FILES}${file}\n"
  elif [[ "$file" =~ \.(md|txt|rst)$ ]]; then
    DOC_FILES="${DOC_FILES}${file}\n"
  elif [[ "$file" =~ \.(yml|yaml|json|toml|cfg|ini|env)$ ]] || \
       [[ "$file" =~ Makefile$ ]] || [[ "$file" =~ Dockerfile$ ]] || \
       [[ "$file" =~ \.github/ ]]; then
    CONFIG_FILES="${CONFIG_FILES}${file}\n"
  else
    SOURCE_FILES="${SOURCE_FILES}${file}\n"
  fi
done <<< "$CHANGED_FILES"

# Build manifest text. Use $'\n' for literal newlines (not \n strings + echo -e) so
# that git-derived filenames with backslash sequences are never interpreted.
MANIFEST="BASE: ${BASE_REF} | DIFF: ${DIFF_LABEL} | LANGUAGES: ${LANGUAGES:-unknown} | FILES: ${FILE_COUNT} | ${DIFF_STAT}"
if [[ -n "$SOURCE_FILES" ]]; then
  MANIFEST+=$'\n\n'"Source: $(set +o pipefail; printf '%s' "$SOURCE_FILES" | head -20 | tr '\n' ', ' | sed 's/,$//')"
fi
if [[ -n "$TEST_FILES" ]]; then
  MANIFEST+=$'\n'"Tests: $(set +o pipefail; printf '%s' "$TEST_FILES" | head -10 | tr '\n' ', ' | sed 's/,$//')"
fi
if [[ -n "$CONFIG_FILES" ]]; then
  MANIFEST+=$'\n'"Config: $(set +o pipefail; printf '%s' "$CONFIG_FILES" | head -10 | tr '\n' ', ' | sed 's/,$//')"
fi
if [[ -n "$DOC_FILES" ]]; then
  MANIFEST+=$'\n'"Docs: $(set +o pipefail; printf '%s' "$DOC_FILES" | head -10 | tr '\n' ', ' | sed 's/,$//')"
fi

# Commit log — scoped to the same range as the diff
if [[ -n "$DIFF_BASE" ]]; then
  COMMIT_LOG=$(set +o pipefail; git log --oneline "${DIFF_BASE}..${HEAD_SHA}" 2>/dev/null | head -20)
else
  COMMIT_LOG=$(set +o pipefail; git log --oneline "origin/${BASE_REF}..${HEAD_SHA}" 2>/dev/null | head -20)
fi

# Validate and log review mode
if [[ "$REVIEW_MODE" != "quick" && "$REVIEW_MODE" != "full" ]]; then
  echo "WARNING: Unknown AI_REVIEW_MODE '${REVIEW_MODE}'. Defaulting to quick." >&2
  REVIEW_MODE="quick"
fi

# Compute diff size for informational logging (and large-diff warning).
# grep fails when the pattern is absent (pure adds have no deletions, etc.),
# so || echo "0" is intentional — parse failures default to 0 lines, which is
# the safe direction (does not suppress any review output).
TOTAL_CHANGED=$(echo "$DIFF_STAT" | grep -oE '[0-9]+ insertions?' | grep -o '[0-9]*' 2>/dev/null || echo "0")
TOTAL_REMOVED=$(echo "$DIFF_STAT" | grep -oE '[0-9]+ deletions?' | grep -o '[0-9]*' 2>/dev/null || echo "0")
if [[ "${TOTAL_CHANGED:-0}" == "0" && "${TOTAL_REMOVED:-0}" == "0" && -n "$DIFF_STAT" ]]; then
  echo "NOTE: Could not parse insertion/deletion counts from diff stat; defaulting to 0. Stat: ${DIFF_STAT}" >&2
fi
TOTAL_LINES=$(( ${TOTAL_CHANGED:-0} + ${TOTAL_REMOVED:-0} ))

if [[ "$TOTAL_LINES" -gt 2000 ]]; then
  echo "WARNING: Large diff (${TOTAL_LINES} changed lines). Consider reviewing incrementally." >&2
fi

# Load language profile(s)
LANGUAGE_CONTEXT=""
for lang in "${DETECTED_LANGS[@]+"${DETECTED_LANGS[@]}"}"; do
  lang_lower=$(echo "$lang" | tr '[:upper:]' '[:lower:]')
  profile="${SCRIPT_DIR}/language-profiles/${lang_lower}.md"
  if [[ -f "$profile" ]]; then
    LANGUAGE_CONTEXT+=$'\n'"$(cat "$profile")"$'\n'
  fi
done

# Read project context (CLAUDE.md) if available
PROJECT_CONTEXT=""
if [[ -f "CLAUDE.md" ]]; then
  # Extract first ~500 tokens (~2000 chars) of project context
  PROJECT_CONTEXT=$(head -c 2000 CLAUDE.md)
  if [[ $(wc -c < CLAUDE.md) -gt 2000 ]]; then
    echo "NOTE: CLAUDE.md truncated to 2000 chars for agent context." >&2
  fi
fi

# ---------------------------------------------------------------------------
# Phase 1: Prepare agent messages and call agents
# ---------------------------------------------------------------------------
echo "--- Calling agents ---" >&2

# --- Build shared message files ---

# Full context message: manifest + commit log + project context + language context + diff
FULL_CONTEXT_MSG=$(mktemp_tracked /tmp/ai-review-full-ctx-XXXXXXXX.md)
{
  echo "## File Manifest"
  printf '%s\n' "$MANIFEST"
  echo ""
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

# Code context message: manifest + language context + diff (no commit log/project context)
CODE_CONTEXT_MSG=$(mktemp_tracked /tmp/ai-review-code-ctx-XXXXXXXX.md)
{
  echo "## File Manifest"
  printf '%s\n' "$MANIFEST"
  echo ""
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

# --- Helper: call agent and handle failure ---
# Intercepts TOKENS: lines from llm-call.sh stderr for usage tracking;
# forwards all other stderr to the workflow log.
call_agent() {
  local name="$1" model="$2" prompt="$3" msg="$4" output="$5" max_tokens="${6:-16384}"
  echo "$name" > "${output}.name"
  echo "Calling ${name} (${model##*.})..." >&2

  local agent_stderr
  agent_stderr=$(mktemp_tracked /tmp/ai-review-stderr-XXXXXXXX.txt)

  local exit_code=0
  "${SCRIPT_DIR}/llm-call.sh" "$model" "$prompt" "$msg" "$max_tokens" \
    > "$output" 2> "$agent_stderr" || exit_code=$?
  if [[ "$exit_code" -ne 0 ]]; then
    local last_err failure_type
    last_err=$(grep -m1 'ERROR:' "$agent_stderr" 2>/dev/null || tail -1 "$agent_stderr" 2>/dev/null)
    case "$exit_code" in
      2) failure_type="transient API error, retries exhausted" ;;
      3) failure_type="response blocked by provider content filter" ;;
      *) failure_type="configuration or request error" ;;
    esac
    echo "WARNING: ${name} failed (${failure_type}): ${last_err:-no stderr output}. Continuing without its output." >&2
    cat "$agent_stderr" >&2
    FAILED_AGENTS+=("$name")
    echo "" > "$output"
    return
  fi

  # Parse token usage and truncation lines; forward remaining stderr to workflow log
  local token_line="" was_truncated=false
  while IFS= read -r line; do
    if [[ "$line" == TOKENS:* ]]; then
      token_line="$line"
    elif [[ "$line" == "TRUNCATED:true" ]]; then
      was_truncated=true
    else
      echo "$line" >&2
    fi
  done < "$agent_stderr"
  # Write a sidecar file so extract_findings() knows to attempt JSON repair.
  # Using a separate file avoids any risk of the LLM emitting the sentinel in its prose.
  if [[ "$was_truncated" == "true" ]]; then
    touch "${output}.truncated"
  fi

  if [[ -n "$token_line" ]]; then
    local input_tokens output_tokens model_id
    input_tokens=$(echo "$token_line" | grep -oE 'input=[0-9]+' | sed 's/input=//' || echo "0")
    output_tokens=$(echo "$token_line" | grep -oE 'output=[0-9]+' | sed 's/output=//' || echo "0")
    model_id=$(echo "$token_line" | grep -oE 'model=[^ ]+' | sed 's/model=//' || echo "unknown")
    echo "  tokens: input=${input_tokens} output=${output_tokens} model=${model_id}" >&2
    TOKEN_LOG+=("${name}: input=${input_tokens} output=${output_tokens} model=${model_id}")
  fi
}

# --- Background variant of call_agent for parallel execution ---
# Writes results to sidecar files instead of mutating parent arrays:
#   ${output}.tokens  — token log entry (one line, empty on failure)
#   ${output}.failed  — exists (empty) iff the agent failed
# The .truncated sidecar is written by the same touch as in call_agent.
# Callers must invoke collect_parallel_results() after wait to merge state.
call_agent_bg() {
  local name="$1" model="$2" prompt="$3" msg="$4" output="$5" max_tokens="${6:-16384}"
  echo "Calling ${name} (${model##*.})..." >&2
  # Write agent name so collect_parallel_results can recover it regardless of success/failure
  echo "$name" > "${output}.name"

  local agent_stderr
  agent_stderr=$(mktemp /tmp/ai-review-stderr-XXXXXXXX.txt)

  local exit_code=0
  "${SCRIPT_DIR}/llm-call.sh" "$model" "$prompt" "$msg" "$max_tokens" \
    > "$output" 2> "$agent_stderr" || exit_code=$?

  if [[ "$exit_code" -ne 0 ]]; then
    local last_err failure_type
    last_err=$(grep -m1 'ERROR:' "$agent_stderr" 2>/dev/null || tail -1 "$agent_stderr" 2>/dev/null)
    case "$exit_code" in
      2) failure_type="transient API error, retries exhausted" ;;
      3) failure_type="response blocked by provider content filter" ;;
      *) failure_type="configuration or request error" ;;
    esac
    echo "WARNING: ${name} failed (${failure_type}): ${last_err:-no stderr output}. Continuing without its output." >&2
    cat "$agent_stderr" >&2
    touch "${output}.failed"
    echo "" > "$output"
    rm -f "$agent_stderr"
    return
  fi

  # Parse token usage and truncation lines; forward remaining stderr to workflow log
  local token_line="" was_truncated=false
  while IFS= read -r line; do
    if [[ "$line" == TOKENS:* ]]; then
      token_line="$line"
    elif [[ "$line" == "TRUNCATED:true" ]]; then
      was_truncated=true
    else
      echo "$line" >&2
    fi
  done < "$agent_stderr"
  rm -f "$agent_stderr"

  if [[ "$was_truncated" == "true" ]]; then
    touch "${output}.truncated"
  fi

  if [[ -n "$token_line" ]]; then
    local input_tokens output_tokens model_id
    input_tokens=$(echo "$token_line" | grep -oE 'input=[0-9]+' | sed 's/input=//' || echo "0")
    output_tokens=$(echo "$token_line" | grep -oE 'output=[0-9]+' | sed 's/output=//' || echo "0")
    model_id=$(echo "$token_line" | grep -oE 'model=[^ ]+' | sed 's/model=//' || echo "unknown")
    echo "  tokens: input=${input_tokens} output=${output_tokens} model=${model_id}" >&2
    echo "${name}: input=${input_tokens} output=${output_tokens} model=${model_id}" > "${output}.tokens"
  fi
}

# Wait for a tier of background agents identified by parallel PID and output-file arrays.
# If a subshell exits non-zero but never wrote a .failed sidecar (killed by signal before
# reaching that line), synthesize the sidecar so collect_parallel_results counts it as failed.
# Args: interleaved "pid output_file" pairs: wait_tier_pids p1 f1 p2 f2 ...
wait_tier_pids() {
  local pid f
  while [[ "$#" -ge 2 ]]; do
    pid="$1" f="$2"; shift 2
    wait "$pid" || { [[ ! -f "${f}.failed" ]] && touch "${f}.failed"; }
  done
}

# Collect results from a completed tier of parallel agents into parent arrays.
# Args: roster-ordered list of output file paths (the same $output passed to call_agent_bg).
# Reads ${f}.name, ${f}.failed, ${f}.tokens sidecars; appends to FAILED_AGENTS / TOKEN_LOG.
# Registers sidecars with TMPFILES for cleanup on EXIT.
collect_parallel_results() {
  local output_files=("$@")
  for f in "${output_files[@]}"; do
    # Register sidecars for cleanup
    for ext in name tokens failed truncated; do
      [[ -f "${f}.${ext}" ]] && TMPFILES+=("${f}.${ext}")
    done

    local agent_name
    if [[ -f "${f}.name" ]]; then
      agent_name=$(cat "${f}.name")
    else
      echo "WARNING: Missing .name sidecar for output file ${f}; agent identity unknown." >&2
      agent_name="unknown"
    fi

    if [[ -f "${f}.failed" ]]; then
      FAILED_AGENTS+=("$agent_name")
    fi
    if [[ -f "${f}.tokens" ]]; then
      TOKEN_LOG+=("$(cat "${f}.tokens")")
    fi
  done
}

# --- Build effective prompt path (appends suggestion addendum when enabled) ---
# Called from 10 agent dispatch sites via $(effective_prompt <agent> <base_prompt>).
# Note: the returned path must be registered with TMPFILES in the caller's shell,
# not inside the command substitution — mktemp_tracked's TMPFILES+= mutation is
# lost when it runs in a $(...) subshell. The caller captures the echoed path and
# we rely on the shared /tmp cleanup. On missing addendum, missing base prompt,
# or cat failure, this function falls back to the base prompt with a WARNING
# rather than silently passing a truncated prompt to the LLM.
#
# Fallback behavior for a missing base prompt: the function echoes the missing
# path back verbatim (not a sentinel or empty string). This is intentional — the
# pre-existing flow without this helper passes the base prompt path directly to
# call_agent → llm-call.sh, which already handles "file does not exist" via its
# own exit-1 path. Emitting the missing path here preserves that behavior and
# avoids a second failure mode for callers to handle. Operators see two signals:
# a WARNING from this function plus the downstream "file not found" error from
# the agent invocation.
effective_prompt() {
  local agent_name="$1" base_prompt="$2"
  # Case-insensitive gate so TRUE/True/true all work consistently with post-review.sh.
  local _enable="${AI_ENABLE_SUGGESTIONS:-true}"
  _enable="${_enable,,}"
  if [[ "$_enable" == "true" ]]; then
    case "$agent_name" in
      code-reviewer|edge-case-hunter|security-reviewer|silent-failure-hunter|blind-hunter)
        local addendum="${SCRIPT_DIR}/prompts/suggestion-addendum.md"
        if [[ ! -f "$addendum" ]]; then
          echo "WARNING: Suggestion addendum missing at ${addendum}; using base prompt for ${agent_name}." >&2
          echo "$base_prompt"
          return
        fi
        if [[ ! -f "$base_prompt" ]]; then
          echo "WARNING: Base prompt missing at ${base_prompt}; cannot build effective prompt for ${agent_name}." >&2
          echo "$base_prompt"
          return
        fi
        # Cleaned up in the parent trap via a glob match on EFFECTIVE_PROMPT_PREFIX,
        # since TMPFILES+= would not propagate back through the $(...) call site.
        local combined
        combined=$(mktemp "${EFFECTIVE_PROMPT_PREFIX}-XXXXXXXX.md" 2>/dev/null) || {
          echo "WARNING: Failed to create temp file for ${agent_name} effective prompt; using base prompt." >&2
          echo "$base_prompt"
          return
        }
        if ! cat "$base_prompt" "$addendum" > "$combined" 2>/dev/null; then
          echo "WARNING: Failed to assemble effective prompt for ${agent_name}; using base prompt." >&2
          rm -f "$combined"
          echo "$base_prompt"
          return
        fi
        echo "$combined"
        return
        ;;
    esac
  fi
  echo "$base_prompt"
}

# --- Detect conditional agent triggers ---
HAS_ERROR_PATTERNS=0
if grep -qE '(catch|if err|try \{|rescue|Result<|unwrap|except|\.catch\()' "$DIFF_FILE" 2>/dev/null; then
  HAS_ERROR_PATTERNS=1
fi

# --- Output files ---
SUMMARY_FILE=$(mktemp_tracked /tmp/ai-review-summary-XXXXXXXX.md)
FINDINGS_FILE=$(mktemp_tracked /tmp/ai-review-findings-XXXXXXXX.md)

# --- Configurable limits ---
# Validate AI_MAX_TOKENS_PER_AGENT: must be a positive integer; clamp to [256, 65536].
_raw_tokens="${AI_MAX_TOKENS_PER_AGENT:-8192}"
if [[ "$_raw_tokens" =~ ^[0-9]+$ ]] && [[ "$_raw_tokens" -ge 256 ]] && [[ "$_raw_tokens" -le 65536 ]]; then
  AI_MAX_TOKENS_PER_AGENT="$_raw_tokens"
else
  echo "WARNING: AI_MAX_TOKENS_PER_AGENT='${_raw_tokens}' is invalid; using default 8192." >&2
  AI_MAX_TOKENS_PER_AGENT=8192
fi
unset _raw_tokens

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

  # pr-summarizer: only on first review (gate evaluated in parent)
  if [[ -z "$LAST_REVIEWED_SHA" ]]; then
    TIER1_OUTPUTS+=("$SUMMARY_FILE")
    call_agent_bg "pr-summarizer" "$AI_MODEL_STANDARD" \
      "${SCRIPT_DIR}/prompts/pr-summarizer.md" "$FULL_CONTEXT_MSG" "$SUMMARY_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
    TIER1_WAIT_ARGS+=($! "$SUMMARY_FILE")
  else
    echo "Skipping pr-summarizer (incremental run — summary already posted)." >&2
  fi

  TIER1_OUTPUTS+=("$FINDINGS_FILE")
  call_agent_bg "code-reviewer" "$AI_MODEL_STANDARD" \
    "$(effective_prompt code-reviewer "${SCRIPT_DIR}/prompts/code-reviewer.md")" "$CODE_CONTEXT_MSG" "$FINDINGS_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
  TIER1_WAIT_ARGS+=($! "$FINDINGS_FILE")

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

    ARCH_FILE=$(mktemp_tracked /tmp/ai-review-arch-XXXXXXXX.md)
    TIER2_OUTPUTS+=("$ARCH_FILE")
    call_agent_bg "architecture-reviewer" "$AI_MODEL_PREMIUM" \
      "${SCRIPT_DIR}/prompts/architecture-reviewer.md" "$FULL_CONTEXT_MSG" "$ARCH_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
    TIER2_WAIT_ARGS+=($! "$ARCH_FILE")

    SEC_FILE=$(mktemp_tracked /tmp/ai-review-sec-XXXXXXXX.md)
    TIER2_OUTPUTS+=("$SEC_FILE")
    call_agent_bg "security-reviewer" "$AI_MODEL_PREMIUM" \
      "$(effective_prompt security-reviewer "${SCRIPT_DIR}/prompts/security-reviewer.md")" "$CODE_CONTEXT_MSG" "$SEC_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
    TIER2_WAIT_ARGS+=($! "$SEC_FILE")

    BLIND_FILE=$(mktemp_tracked /tmp/ai-review-blind-XXXXXXXX.md)
    TIER2_OUTPUTS+=("$BLIND_FILE")
    call_agent_bg "blind-hunter" "$AI_MODEL_STANDARD" \
      "$(effective_prompt blind-hunter "${SCRIPT_DIR}/prompts/blind-hunter.md")" "$BLIND_MSG" "$BLIND_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
    TIER2_WAIT_ARGS+=($! "$BLIND_FILE")

    EDGE_FILE=$(mktemp_tracked /tmp/ai-review-edge-XXXXXXXX.md)
    TIER2_OUTPUTS+=("$EDGE_FILE")
    call_agent_bg "edge-case-hunter" "$AI_MODEL_PREMIUM" \
      "$(effective_prompt edge-case-hunter "${SCRIPT_DIR}/prompts/edge-case-hunter.md")" "$CODE_CONTEXT_MSG" "$EDGE_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
    TIER2_WAIT_ARGS+=($! "$EDGE_FILE")

    ADV_FILE=$(mktemp_tracked /tmp/ai-review-adv-XXXXXXXX.md)
    TIER2_OUTPUTS+=("$ADV_FILE")
    call_agent_bg "adversarial-general" "$AI_MODEL_STANDARD" \
      "${SCRIPT_DIR}/prompts/adversarial-general.md" "$CODE_CONTEXT_MSG" "$ADV_FILE" "$AI_MAX_TOKENS_PER_AGENT" &
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
    ARCH_FILE=$(mktemp_tracked /tmp/ai-review-arch-XXXXXXXX.md)
    call_agent "architecture-reviewer" "$AI_MODEL_PREMIUM" \
      "${SCRIPT_DIR}/prompts/architecture-reviewer.md" "$FULL_CONTEXT_MSG" "$ARCH_FILE" "$AI_MAX_TOKENS_PER_AGENT"
    AGENT_OUTPUTS+=("$ARCH_FILE")

    SEC_FILE=$(mktemp_tracked /tmp/ai-review-sec-XXXXXXXX.md)
    call_agent "security-reviewer" "$AI_MODEL_PREMIUM" \
      "$(effective_prompt security-reviewer "${SCRIPT_DIR}/prompts/security-reviewer.md")" "$CODE_CONTEXT_MSG" "$SEC_FILE" "$AI_MAX_TOKENS_PER_AGENT"
    AGENT_OUTPUTS+=("$SEC_FILE")

    BLIND_FILE=$(mktemp_tracked /tmp/ai-review-blind-XXXXXXXX.md)
    call_agent "blind-hunter" "$AI_MODEL_STANDARD" \
      "$(effective_prompt blind-hunter "${SCRIPT_DIR}/prompts/blind-hunter.md")" "$BLIND_MSG" "$BLIND_FILE" "$AI_MAX_TOKENS_PER_AGENT"
    AGENT_OUTPUTS+=("$BLIND_FILE")

    EDGE_FILE=$(mktemp_tracked /tmp/ai-review-edge-XXXXXXXX.md)
    call_agent "edge-case-hunter" "$AI_MODEL_PREMIUM" \
      "$(effective_prompt edge-case-hunter "${SCRIPT_DIR}/prompts/edge-case-hunter.md")" "$CODE_CONTEXT_MSG" "$EDGE_FILE" "$AI_MAX_TOKENS_PER_AGENT"
    AGENT_OUTPUTS+=("$EDGE_FILE")

    ADV_FILE=$(mktemp_tracked /tmp/ai-review-adv-XXXXXXXX.md)
    call_agent "adversarial-general" "$AI_MODEL_STANDARD" \
      "${SCRIPT_DIR}/prompts/adversarial-general.md" "$CODE_CONTEXT_MSG" "$ADV_FILE" "$AI_MAX_TOKENS_PER_AGENT"
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
    in_tok=$(echo "$entry" | grep -oE 'input=[0-9]+' | sed 's/input=//' || echo "0")
    out_tok=$(echo "$entry" | grep -oE 'output=[0-9]+' | sed 's/output=//' || echo "0")
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

# Extract json-findings from each agent output, validate shape, and return clean array.
extract_findings() {
  local agent_file="$1"
  local agent_name="${2:-unknown}"
  local was_truncated=false
  [[ -f "${agent_file}.truncated" ]] && was_truncated=true

  if ! grep -q '```json-findings' "$agent_file" 2>/dev/null; then
    if [[ "$was_truncated" == "true" ]]; then
      echo "WARNING: $(basename "$agent_file") was truncated before json-findings block; findings lost." >&2
    else
      echo "WARNING: $(basename "$agent_file") has no json-findings block; skipping." >&2
    fi
    echo "[]"
    return
  fi

  local extracted
  # Single quotes are intentional — $ is sed's end-of-range/last-line anchor,
  # not a shell expansion. Same rationale below at the FINDINGS_CLEAN_FILE loop.
  # shellcheck disable=SC2016
  extracted=$(sed -n '/```json-findings/,/```/p' "$agent_file" | sed '1d;$d')

  # Stamp source on findings that don't already carry one, then validate.
  # Returns the stamped array on success; exits non-zero on invalid input.
  _ef_stamp_and_validate() {
    local raw="$1"
    echo "$raw" | jq -e --arg s "$agent_name" '
      if (type == "array") and
         (if length > 0 then all(.[]; has("severity") and has("finding") and has("confidence")) else true end)
      then map(. + {source: (.source // $s)})
      else error("invalid")
      end
    ' 2>/dev/null
  }

  local stamped
  if stamped=$(_ef_stamp_and_validate "$extracted"); then
    printf '%s' "$stamped"
    return
  fi

  # Validation failed — if the response was truncated, attempt to repair the JSON.
  # Strategy: iterate over closing-brace lines from last to first, close the array
  # at each candidate, and accept the first result that passes schema validation.
  # Iterating (rather than taking the last '}') handles nested objects whose inner
  # '}' lines would otherwise produce invalid JSON.
  if [[ "$was_truncated" == "true" ]]; then
    local candidate_line repaired
    while IFS=: read -r candidate_line _; do
      repaired=$(printf '%s' "$extracted" | head -n "$candidate_line" | sed '$ s/,$//')
      repaired=$(printf '%s\n]' "$repaired")
      if stamped=$(_ef_stamp_and_validate "$repaired"); then
        local count
        count=$(printf '%s' "$stamped" | jq 'length')
        echo "NOTE: $(basename "$agent_file") was truncated; salvaged ${count} finding(s) from partial JSON." >&2
        printf '%s' "$stamped"
        return
      fi
    done < <(printf '%s' "$extracted" | grep -n '^[[:space:]]*}' | tac)
  fi

  local preview
  preview=$(set +o pipefail; printf '%s' "$extracted" | head -c 200)
  echo "WARNING: $(basename "$agent_file") produced invalid or malformed json-findings; skipping. Preview: ${preview}" >&2
  echo "[]"
}

merge_findings() {
  local incoming="$1"
  # Validate and filter incoming findings element-by-element so a single malformed
  # object doesn't discard the rest of the batch. Required fields: severity, finding,
  # confidence. Missing file or line is accepted (body-only findings).
  local valid_incoming
  valid_incoming=$(echo "$incoming" | jq '[.[] | select(
    (.severity | type) == "string" and
    (.finding  | type) == "string" and
    (.confidence | type) == "number"
  )]' 2>/dev/null) || valid_incoming="[]"

  local incoming_count valid_count
  incoming_count=$(echo "$incoming" | jq 'length' 2>/dev/null || echo "0")
  valid_count=$(echo "$valid_incoming" | jq 'length' 2>/dev/null || echo "0")
  if [[ "$valid_count" != "$incoming_count" ]]; then
    echo "WARNING: merge_findings: dropped $(( incoming_count - valid_count )) malformed finding(s) (missing required fields)." >&2
  fi

  if [[ "$valid_count" == "0" ]]; then
    return 0
  fi

  if jq -s '.[0] + .[1]' "$FINDINGS_JSON_FILE" <(echo "$valid_incoming") > "${FINDINGS_JSON_FILE}.tmp" 2>/dev/null; then
    mv "${FINDINGS_JSON_FILE}.tmp" "$FINDINGS_JSON_FILE"
  else
    echo "WARNING: Failed to merge findings JSON; skipping batch of ${valid_count} finding(s)." >&2
    rm -f "${FINDINGS_JSON_FILE}.tmp"
  fi
}

# Apply declarative suppressions from suppressions.json.
# Runs after all findings are merged, before confidence filter and dedup.
# Suppressed findings are removed from FINDINGS_JSON_FILE and logged to stderr.
# Local suppressions in ${GITHUB_WORKSPACE}/.github/ai-pr-review/suppressions.json
# are merged with the global rules (local rules take precedence in log output).
apply_suppressions() {
  local suppressions_file="${SCRIPT_DIR}/config/suppressions.json"
  local local_suppressions_file="${GITHUB_WORKSPACE:-}/.github/ai-pr-review/suppressions.json"
  local combined_rules_file
  combined_rules_file=$(mktemp /tmp/ai-review-suppressions-XXXXXXXX.json)
  TMPFILES+=("$combined_rules_file")

  if [[ ! -f "$suppressions_file" ]]; then
    return 0
  fi

  if ! jq -e 'type == "array"' "$suppressions_file" > /dev/null 2>&1; then
    echo "WARNING: suppressions.json is not a valid JSON array; skipping suppression filter." >&2
    return 0
  fi

  if [[ -n "${GITHUB_WORKSPACE:-}" && -f "$local_suppressions_file" ]]; then
    if ! jq -e 'type == "array"' "$local_suppressions_file" > /dev/null 2>&1; then
      echo "WARNING: local suppressions.json is not a valid JSON array; ignoring local suppressions." >&2
      cp "$suppressions_file" "$combined_rules_file"
    else
      jq -s 'add' "$suppressions_file" "$local_suppressions_file" > "$combined_rules_file"
      echo "Loaded local suppressions from .github/ai-pr-review/suppressions.json" >&2
    fi
  else
    cp "$suppressions_file" "$combined_rules_file"
  fi

  local result
  result=$(jq --slurpfile rules "$combined_rules_file" '
    ($rules[0]) as $active_rules |

    def rule_matches(rule):
      (if rule.match.code then
        (.finding | startswith(rule.match.code))
      else true end)
      and
      (if rule.match.file then
        (.file // "" | contains(rule.match.file))
      else true end)
      and
      (if rule.match.line then
        (.line == rule.match.line)
      else true end)
      and
      (if rule.match.pattern then
        (.finding | test(rule.match.pattern; "i"))
      else true end);

    def find_rule_id:
      . as $f |
      ([$active_rules[] | select(. as $r | $f | rule_matches($r))][0].id // "?");

    def find_verify_field:
      . as $f |
      ([$active_rules[] | select(. as $r | $f | rule_matches($r))][0].verify // "") ;

    def is_suppressed:
      . as $finding |
      any($active_rules[]; . as $rule | $finding | rule_matches($rule));

    {
      kept: [.[] | select(is_suppressed | not)],
      suppressed: [.[] | select(is_suppressed) | . + {suppression_id: find_rule_id, verify: find_verify_field}]
    }
  ' "$FINDINGS_JSON_FILE") || {
    echo "WARNING: apply_suppressions jq failed; skipping suppression filter." >&2
    return 0
  }

  # For findings suppressed by rules with a verify field, confirm the version exists
  # via the appropriate registry API before accepting the suppression. If the version
  # genuinely does not exist (404), keep the finding — the AI may be correct after all.
  local verified_suppressed
  verified_suppressed=$(echo "$result" | jq '.suppressed')

  # Restored findings (failed verification) are written to a temp file as newline-
  # delimited JSON objects, then assembled into an array once after the loop.
  local restored_tmp
  restored_tmp=$(mktemp /tmp/ai-review-restored-XXXXXXXX.jsonl)
  TMPFILES+=("$restored_tmp")

  # Namespaced helper — removes a finding from verified_suppressed.
  # (bash functions are global; the prefix prevents collisions.)
  _suppression_restore() {
    local fj="$1"
    echo "$fj" | jq 'del(.suppression_id, .verify)' >> "$restored_tmp"
    verified_suppressed=$(echo "$verified_suppressed" | jq --argjson f "$fj" \
      '[.[] | select(.finding != $f.finding or .file != $f.file or .line != $f.line)]')
  }

  while IFS= read -r finding_json; do
    local verify_type finding_text
    verify_type=$(echo "$finding_json" | jq -r '.verify // ""')
    [[ -z "$verify_type" ]] && continue
    finding_text=$(echo "$finding_json" | jq -r '.finding')

    case "$verify_type" in

      github-release)
        # Extract owner/repo@vN.N.N — captures both major tags (v6) and patch tags (v4.1.0)
        local action_ref owner_repo tag
        action_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+@v[0-9]+(\.[0-9]+)*' | head -1)
        if [[ -z "$action_ref" ]]; then continue; fi
        owner_repo="${action_ref%@*}"; tag="${action_ref#*@}"
        echo "Verifying GitHub release: ${owner_repo}@${tag}" >&2
        if gh api "repos/${owner_repo}/git/ref/tags/${tag}" > /dev/null 2>&1 \
           || gh api "repos/${owner_repo}/releases/tags/${tag}" > /dev/null 2>&1; then
          echo "  Confirmed tag/release exists — suppressing finding." >&2
        else
          echo "  Version not found via GitHub API — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      npm)
        # Extract @scope/pkg@version or pkg@version, or "pkg": "version" in JSON.
        # Scoped packages (@scope/pkg) are URL-encoded as %40scope%2Fpkg for the registry.
        local npm_pkg npm_ver npm_pkg_enc
        local npm_ref
        # Try scoped package first: @scope/pkg@version
        npm_ref=$(echo "$finding_text" | grep -oE '@[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+@[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -n "$npm_ref" ]]; then
          npm_pkg="${npm_ref%@[0-9]*}"; npm_ver="${npm_ref##*@}"
        else
          # Unscoped: pkg@version
          npm_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+@[0-9][0-9a-zA-Z._-]*' | head -1)
          if [[ -n "$npm_ref" ]]; then
            npm_pkg="${npm_ref%@*}"; npm_ver="${npm_ref#*@}"
          else
            # "pkg": "version" in package.json snippet
            npm_ref=$(echo "$finding_text" | grep -oP '"([a-zA-Z0-9._-]+)":\s*"([0-9][^"]+)"' | head -1)
            npm_pkg=$(echo "$npm_ref" | grep -oP '(?<=")[a-zA-Z0-9._-]+(?=":\s*")')
            npm_ver=$(echo "$npm_ref" | grep -oP '(?<=":\s*")[0-9][^"]+')
          fi
        fi
        if [[ -z "$npm_pkg" || -z "$npm_ver" ]]; then continue; fi
        # URL-encode scoped package names: @scope/pkg -> %40scope%2Fpkg
        npm_pkg_enc="${npm_pkg//@/%40}"
        npm_pkg_enc="${npm_pkg_enc//\//%2F}"
        echo "Verifying npm: ${npm_pkg}@${npm_ver}" >&2
        if curl -sf "https://registry.npmjs.org/${npm_pkg_enc}/${npm_ver}" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on npm — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      pypi)
        # Extract pkg==version (e.g. "requests==3.0.0") or "pkg version X.Y.Z"
        local pypi_ref pypi_pkg pypi_ver
        pypi_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+==[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -n "$pypi_ref" ]]; then
          pypi_pkg="${pypi_ref%%==*}"; pypi_ver="${pypi_ref##*==}"
        else
          pypi_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+ version [0-9][0-9a-zA-Z._-]*' | head -1)
          pypi_pkg=$(echo "$pypi_ref" | awk '{print $1}')
          pypi_ver=$(echo "$pypi_ref" | awk '{print $3}')
        fi
        if [[ -z "$pypi_pkg" || -z "$pypi_ver" ]]; then continue; fi
        echo "Verifying PyPI: ${pypi_pkg}==${pypi_ver}" >&2
        if curl -sf "https://pypi.org/pypi/${pypi_pkg}/${pypi_ver}/json" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on PyPI — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      go-module)
        # Extract module@vX.Y.Z (e.g. "github.com/gin-gonic/gin@v1.10.0").
        # Go module proxy requires: slashes encoded as %2F, uppercase letters as
        # !lowercase (e.g. BurntSushi -> !burnt!sushi).
        local go_ref go_mod go_ver go_mod_enc go_char
        go_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}/[a-zA-Z0-9._/@-]+@v[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -z "$go_ref" ]]; then continue; fi
        go_mod="${go_ref%@*}"; go_ver="${go_ref#*@}"
        # Apply Go module proxy encoding: uppercase A-Z -> !lowercase, / -> %2F
        go_mod_enc=""
        while IFS= read -r -n1 go_char; do
          if [[ "$go_char" =~ [A-Z] ]]; then
            go_mod_enc+="!${go_char,,}"
          elif [[ "$go_char" == "/" ]]; then
            go_mod_enc+="%2F"
          else
            go_mod_enc+="$go_char"
          fi
        done <<< "$go_mod"
        # Remove trailing newline added by herestring
        go_mod_enc="${go_mod_enc%$'\n'}"
        echo "Verifying Go module: ${go_mod}@${go_ver}" >&2
        if curl -sf "https://proxy.golang.org/${go_mod_enc}/@v/${go_ver}.info" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on Go module proxy — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      cargo)
        # Extract pkg = "version" or pkg@version (e.g. serde = "1.1.0" or tokio@2.0.0)
        local cargo_pkg cargo_ver cargo_ref
        cargo_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9_-]+ = "[0-9][^"]*"' | head -1)
        if [[ -n "$cargo_ref" ]]; then
          cargo_pkg=$(echo "$cargo_ref" | grep -oE '^[a-zA-Z0-9_-]+')
          cargo_ver=$(echo "$cargo_ref" | grep -oE '"[0-9][^"]*"' | tr -d '"')
        else
          cargo_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9_-]+@[0-9][0-9a-zA-Z._-]*' | head -1)
          cargo_pkg="${cargo_ref%@*}"; cargo_ver="${cargo_ref#*@}"
        fi
        if [[ -z "$cargo_pkg" || -z "$cargo_ver" ]]; then continue; fi
        echo "Verifying Cargo: ${cargo_pkg}@${cargo_ver}" >&2
        if curl -sf "https://crates.io/api/v1/crates/${cargo_pkg}/${cargo_ver}" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on crates.io — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      docker-hub)
        # Extract image:tag (e.g. "nginx:1.27.0" or "bitnami/postgresql:17.4.0").
        # Uses /v2/namespaces/ endpoint which supports anonymous access for public images.
        local docker_ref docker_img docker_tag docker_ns docker_name
        docker_ref=$(echo "$finding_text" | grep -oE '([a-zA-Z0-9._-]+/)?[a-zA-Z0-9._-]+:[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -z "$docker_ref" ]]; then continue; fi
        docker_img="${docker_ref%:*}"; docker_tag="${docker_ref#*:}"
        if [[ "$docker_img" == */* ]]; then
          docker_ns="${docker_img%/*}"; docker_name="${docker_img#*/}"
        else
          docker_ns="library"; docker_name="$docker_img"
        fi
        echo "Verifying Docker Hub: ${docker_ns}/${docker_name}:${docker_tag}" >&2
        if curl -sf "https://hub.docker.com/v2/namespaces/${docker_ns}/repositories/${docker_name}/tags/${docker_tag}" > /dev/null 2>&1; then
          echo "  Confirmed tag exists — suppressing finding." >&2
        else
          echo "  Tag not found on Docker Hub — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

    esac
  done < <(echo "$result" | jq -c '.suppressed[]' 2>/dev/null)

  # Build verified_kept from the temp file (single jq pass, O(n) not O(n²))
  local verified_kept
  if [[ -s "$restored_tmp" ]]; then
    verified_kept=$(jq -s '.' "$restored_tmp")
  else
    verified_kept="[]"
  fi

  # Merge verified_kept back with the originally kept findings; // [] guards against
  # jq -s 'add' returning null when both inputs are empty arrays.
  local final_kept
  final_kept=$(jq -s 'add // []' <(echo "$result" | jq '.kept') <(echo "$verified_kept"))

  if echo "$final_kept" > "${FINDINGS_JSON_FILE}.tmp"; then
    mv "${FINDINGS_JSON_FILE}.tmp" "$FINDINGS_JSON_FILE"
  else
    echo "WARNING: apply_suppressions failed to write filtered findings; keeping original." >&2
    rm -f "${FINDINGS_JSON_FILE}.tmp"
  fi

  local suppressed_count
  suppressed_count=$(echo "$verified_suppressed" | jq 'length')
  SUPPRESSED_COUNT=$suppressed_count

  if [[ "$suppressed_count" -gt 0 ]]; then
    echo "Suppressed findings: ${suppressed_count}" >&2
    echo "$verified_suppressed" | jq -r '.[] | "  SUPPRESSED [\(.suppression_id)] \(.file // "?"):\(.line // "?") — \(.finding | .[0:80])"' >&2
  fi
}

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
MODEL_PRICING_FILE="${SCRIPT_DIR}/config/model-pricing.json"
model_pricing() {
  local model="$1"
  local m result
  m=$(echo "$model" | tr '[:upper:]' '[:lower:]')
  result=$(jq -r --arg m "$m" '
    first(
      .[] | select(.patterns[] as $p | ($m | test($p)))
      | "\(.input_rate) \(.output_rate)"
    ) // "0 0"
  ' "$MODEL_PRICING_FILE" 2>/dev/null) || result="0 0"
  echo "${result:-0 0}"
}

# Human-readable model display name: "Sonnet 4.6", "GPT-4o mini", etc.
model_display_name() {
  local model="$1"
  local m result
  m=$(echo "$model" | tr '[:upper:]' '[:lower:]')
  result=$(jq -r --arg m "$m" '
    first(
      .[] | select(.patterns[] as $p | ($m | test($p)))
      | .display_name
    ) // ""
  ' "$MODEL_PRICING_FILE" 2>/dev/null) || result=""
  echo "${result:-$model}"
}

# Format microdollars (millionths of a dollar) as $X.XXXXXX
format_cost() {
  local microdollars="$1"
  # microdollars = tokens * rate / 1_000_000, where rate is in nanodollars/token * 1e6
  # Actually our unit: rate is in units of $0.000001 per token × 1e6 = dollars/token × 1e12
  # Simpler: pass raw integer from calculation and format as dollars with 4 decimal places
  # We work in units of $0.0001 (tenths of a cent) to keep integers manageable
  local whole=$(( microdollars / 10000 ))
  local frac=$(( microdollars % 10000 ))
  printf '$%d.%04d' "$whole" "$frac"
}

# ---------------------------------------------------------------------------
# Emit token usage table rows to stdout.
# Shared by the PR comment <details> block and the step summary.
# Uses awk for cost arithmetic to avoid bash integer overflow on large
# token counts multiplied by rate constants (up to 75,000,000).
# ---------------------------------------------------------------------------
emit_token_table_rows() {
  local total_in=0 total_out=0 total_cost=0 any_unknown=0
  for entry in "${TOKEN_LOG[@]}"; do
    local agent_name in_tok out_tok model_id row_total in_rate out_rate cost_display model_short
    agent_name="${entry%%:*}"
    in_tok=$(echo "$entry" | grep -oE 'input=[0-9]+' | sed 's/input=//' || echo "0")
    out_tok=$(echo "$entry" | grep -oE 'output=[0-9]+' | sed 's/output=//' || echo "0")
    model_id=$(echo "$entry" | grep -oE 'model=[^ ]+' | sed 's/model=//' || echo "unknown")
    row_total=$(( in_tok + out_tok ))
    read -r in_rate out_rate <<< "$(model_pricing "$model_id")"
    if [[ "$in_rate" -eq 0 && "$out_rate" -eq 0 ]]; then
      cost_display="n/a"
      any_unknown=1
    else
      # Use awk to avoid bash integer overflow (in_tok * in_rate can exceed 2^63 for large runs)
      local cost_units
      cost_units=$(awk -v it="${in_tok:-0}" -v ir="${in_rate:-0}" -v ot="${out_tok:-0}" -v or_="${out_rate:-0}" \
        'BEGIN {printf "%d", (it * ir + ot * or_) / 100000000}')
      cost_display=$(format_cost "$cost_units")
      total_cost=$(( total_cost + cost_units ))
    fi
    model_short=$(model_display_name "$model_id")
    echo "| ${agent_name} | ${model_short} | ${in_tok} | ${out_tok} | ${row_total} | ${cost_display} |"
    total_in=$(( total_in + in_tok ))
    total_out=$(( total_out + out_tok ))
  done
  local total_cost_display
  if [[ "$any_unknown" -eq 1 ]]; then
    total_cost_display="$(format_cost "$total_cost")+"
  else
    total_cost_display="$(format_cost "$total_cost")"
  fi
  echo "| **Total** | | **${total_in}** | **${total_out}** | **$(( total_in + total_out ))** | **${total_cost_display}** |"
}

# Build token usage table as a collapsed details block
TOKEN_TABLE_FILE=$(mktemp_tracked /tmp/ai-review-token-table-XXXXXXXX.md)
if [[ "${#TOKEN_LOG[@]}" -gt 0 ]]; then
  {
    echo "<details>"
    echo "<summary>Token usage by agent</summary>"
    echo ""
    echo "| Agent | Model | Input | Output | Total | Est. Cost |"
    echo "|-------|-------|------:|-------:|------:|----------:|"
    emit_token_table_rows
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
      echo "| Agent | Model | Input | Output | Total | Est. Cost |"
      echo "|-------|-------|------:|-------:|------:|----------:|"
      emit_token_table_rows
      echo ""
      echo "_Prices are public list rates and do not reflect discounts, commitments, or proxy markups._"
      echo ""
    fi
    echo "### Summary"
    cat "$SUMMARY_FILE"
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo "=== AI PR Review complete ===" >&2
