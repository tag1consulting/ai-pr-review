#!/usr/bin/env bash
# lib/diff.sh — diff-size guard, manifest building, and context assembly for review.sh.
#
# Sourced by review.sh (via main() after SCRIPT_DIR is set). Exports:
#   post_skip_comment    — post/update a "diff too large" comment to the VCS provider
#   build_file_manifest  — build the categorized file manifest and language context
#
# Contract: caller must set VCS_PROVIDER, PR_NUMBER, and the standard env vars
# for the target VCS provider before calling post_skip_comment. build_file_manifest
# requires CHANGED_FILES, DIFF_STAT, DIFF_LABEL, BASE_REF, SCRIPT_DIR, and
# detect_language/is_test_file from lib/languages.sh.

# Post (or update) a "diff too large — skipping review" comment to the PR/MR.
# Args: $1 = diff_lines, $2 = max_diff_lines
post_skip_comment() {
  local diff_lines="$1" max_diff_lines="$2"
  local SKIP_MARKER="<!-- ai-pr-review-skipped -->"
  local SKIP_BODY="${SKIP_MARKER}
## AI Review Skipped

This PR's diff is too large for automated review (${diff_lines} lines; limit: ${max_diff_lines}).

To review anyway, increase \`MAX_DIFF_LINES\` in the workflow or split this PR into smaller changes."

  if [[ "$VCS_PROVIDER" == "github" ]]; then
    : "${GH_TOKEN:?GH_TOKEN is required}"
    : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
    : "${PR_NUMBER:?PR_NUMBER is required}"
    local OWNER="${GITHUB_REPOSITORY%%/*}"
    local REPO="${GITHUB_REPOSITORY##*/}"
    local existing_skip_id
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
  elif [[ "$VCS_PROVIDER" == "gitlab" ]]; then
    : "${PR_NUMBER:?PR_NUMBER is required}"
    local _GL_API="${GITLAB_API_URL:-https://gitlab.com/api/v4}"
    local _GL_AUTH=""
    if [[ -n "${GITLAB_TOKEN:-}" ]]; then
      if [[ "$GITLAB_TOKEN" == glpat-* ]]; then
        _GL_AUTH="PRIVATE-TOKEN: ${GITLAB_TOKEN}"
      elif [[ "$GITLAB_TOKEN" == glcbt-* ]]; then
        _GL_AUTH="JOB-TOKEN: ${GITLAB_TOKEN}"
      else
        _GL_AUTH="Authorization: Bearer ${GITLAB_TOKEN}"
      fi
    elif [[ -n "${CI_JOB_TOKEN:-}" ]]; then
      _GL_AUTH="JOB-TOKEN: ${CI_JOB_TOKEN}"
    fi
    if [[ -n "$_GL_AUTH" ]]; then
      local _GL_PROJECT_ID="${GITLAB_PROJECT_ID:-${CI_PROJECT_ID:-}}"
      if [[ -z "$_GL_PROJECT_ID" ]]; then
        _GL_PROJECT_ID=$(printf '%s' "${CI_PROJECT_PATH:-${GITHUB_REPOSITORY:-}}" | sed 's|/|%2F|g')
      fi
      if [[ -n "$_GL_PROJECT_ID" ]]; then
        local existing_skip_id
        existing_skip_id=$(curl -sS -H "$_GL_AUTH" \
          "${_GL_API}/projects/${_GL_PROJECT_ID}/merge_requests/${PR_NUMBER}/notes?per_page=100&sort=desc&order_by=updated_at" \
          2>/dev/null | jq -r --arg marker "$SKIP_MARKER" \
          '[.[] | select((.body // "") | contains($marker))] | first.id // empty' 2>/dev/null) || true
        if [[ -n "$existing_skip_id" ]]; then
          curl -sS -X PUT -H "$_GL_AUTH" \
            --data-urlencode "body=${SKIP_BODY}" \
            "${_GL_API}/projects/${_GL_PROJECT_ID}/merge_requests/${PR_NUMBER}/notes/${existing_skip_id}" \
            > /dev/null 2>&1 || echo "WARNING: Could not update skip note on GitLab MR." >&2
        else
          curl -sS -X POST -H "$_GL_AUTH" \
            --data-urlencode "body=${SKIP_BODY}" \
            "${_GL_API}/projects/${_GL_PROJECT_ID}/merge_requests/${PR_NUMBER}/notes" \
            > /dev/null 2>&1 || echo "WARNING: Could not post skip note to GitLab MR." >&2
        fi
      fi
    fi
  fi
  # Bitbucket: no skip comment posted (by design); the log_warn output
  # is visible in the pipeline log.
}


# Filter out merge commits that pulled upstream base-branch changes into the PR branch,
# leaving only the PR author's own commits. Writes the filtered diff to DIFF_FILE.
#
# Args: $1 = diff_base (origin/<BASE_REF> or watermark SHA)
#       $2 = head_sha
#
# Globals read: BASE_REF (the PR's target branch), DIFF_FILE (written by caller before this call)
# Globals written: AI_MERGE_FILTER_FALLBACK_REASON (non-empty on fallback)
#
# Returns:
#   0 — success (DIFF_FILE updated in-place, or no qualifying merges found — no-op)
#   1 — cherry-pick conflict; AI_MERGE_FILTER_FALLBACK_REASON set; DIFF_FILE untouched
compute_filtered_diff() {
  local diff_base="$1" head_sha="$2"

  # 1. List merge commits in the range
  local merges
  merges=$(git rev-list --merges "${diff_base}..${head_sha}" 2>/dev/null) || merges=""
  if [[ -z "$merges" ]]; then
    return 0  # no merges in range — no-op
  fi

  # 2. Filter to merges whose second parent (M^2) is reachable from origin/<BASE_REF>.
  #    These are "merge main into feature-branch" commits; intra-PR merges have M^2
  #    that is NOT reachable from origin/<BASE_REF>.
  local qualifying=()
  while IFS= read -r m; do
    local p2
    p2=$(git rev-parse "${m}^2" 2>/dev/null) || continue
    if git merge-base --is-ancestor "$p2" "origin/${BASE_REF}" 2>/dev/null; then
      qualifying+=("$m")
    fi
  done <<< "$merges"

  if [[ ${#qualifying[@]} -eq 0 ]]; then
    return 0  # no base-branch merges to filter — no-op
  fi

  echo "Merge-commit filter: found ${#qualifying[@]} upstream merge(s); cherry-picking author commits into synthetic branch." >&2

  # 3. Cherry-pick all non-merge commits from the range onto a temp worktree
  #    rooted at diff_base.
  # Use a path that does not yet exist so git worktree add creates it atomically.
  # $$ (PID) + $RANDOM avoids the TOCTOU race from mktemp -d + rm -rf.
  local worktree_path="/tmp/ai-review-filter-wt-$$-${RANDOM}"
  if ! git worktree add --quiet --detach "$worktree_path" "$diff_base" 2>/dev/null; then
    echo "WARNING: merge-commit filter: could not create git worktree; falling back to unfiltered diff." >&2
    export AI_MERGE_FILTER_FALLBACK_REASON="could not create git worktree for merge-commit filtering"
    return 1
  fi

  # Exclude commits reachable from origin/<BASE_REF> — these are upstream commits
  # that were brought in by the merge(s). --not origin/<BASE_REF> removes them.
  local commits
  commits=$(git rev-list --reverse --no-merges "${diff_base}..${head_sha}" --not "origin/${BASE_REF}" 2>/dev/null) || commits=""

  local pick_failed=0
  if [[ -n "$commits" ]]; then
    # Use || pick_failed=$? so set -e in the parent does not abort before the
    # graceful fallback path can run.
    (
      cd "$worktree_path" || exit 1
      while IFS= read -r c; do
        if ! git cherry-pick --no-commit "$c" 2>/dev/null; then
          git cherry-pick --abort 2>/dev/null || true
          exit 1
        fi
        # Commit preserving original authorship; --allow-empty handles no-change commits
        if ! git commit --no-edit --allow-empty -C "$c" 2>/dev/null; then
          exit 1
        fi
      done <<< "$commits"
    ) || pick_failed=$?
  fi

  if [[ "$pick_failed" -ne 0 ]]; then
    git worktree remove --force "$worktree_path" 2>/dev/null || true
    git worktree prune 2>/dev/null || true
    echo "WARNING: merge-commit filter: cherry-pick conflict; falling back to unfiltered diff." >&2
    export AI_MERGE_FILTER_FALLBACK_REASON="cherry-pick conflict during merge-commit filtering"
    return 1
  fi

  # 4. Write the filtered diff to DIFF_FILE, applying the same exclusion patterns
  #    used in the normal diff path so lockfiles/vendor dirs are never included.
  local synthetic_tip
  synthetic_tip=$(git -C "$worktree_path" rev-parse HEAD 2>/dev/null) || synthetic_tip=""
  if [[ -z "$synthetic_tip" || "$synthetic_tip" == "$diff_base" ]]; then
    # All commits in range were upstream-only or merge commits; nothing to cherry-pick.
    # Write an empty diff so the caller's empty-diff guard triggers a clean skip.
    echo "Merge-commit filter: all commits were upstream-only; filtered diff is empty." >&2
    : > "$DIFF_FILE"
  else
    git diff "${diff_base}..${synthetic_tip}" -- "${EXCL[@]}" > "$DIFF_FILE" 2>/dev/null || : > "$DIFF_FILE"
  fi

  git worktree remove --force "$worktree_path" 2>/dev/null || true
  git worktree prune 2>/dev/null || true
  echo "Merge-commit filter: synthetic diff written (${diff_base:0:7}..${synthetic_tip:0:7})." >&2
  return 0
}


# Build the file manifest, detect languages, categorize files, and load
# language profiles. Sets the following globals:
#   LANGUAGES, DETECTED_LANGS, FILE_COUNT, SOURCE_FILES, TEST_FILES,
#   CONFIG_FILES, DOC_FILES, MANIFEST, TOTAL_CHANGED, TOTAL_REMOVED,
#   TOTAL_LINES, LANGUAGE_CONTEXT, PROJECT_CONTEXT, COMMIT_LOG
#
# Reads: CHANGED_FILES, DIFF_STAT, DIFF_LABEL, DIFF_BASE, BASE_REF,
#        HEAD_SHA, SCRIPT_DIR
#
# Returns 1 if CHANGED_FILES is empty (caller should exit 0).
#
# shellcheck disable=SC2034  # many globals are set here, read by the caller
build_file_manifest() {
  if [[ -z "$CHANGED_FILES" ]]; then
    echo "No changed files after exclusions. Skipping review." >&2
    return 1
  fi
  FILE_COUNT=$(echo "$CHANGED_FILES" | wc -l | tr -d ' ')

  # Detect languages from extensions
  LANGUAGES=""
  DETECTED_LANGS=()
  while IFS= read -r file; do
    local ext="${file##*.}"
    local lang
    lang=$(detect_language "$ext")
    if [[ -n "$lang" ]]; then
      local found=0
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

  # Commit log — scoped to the same range as the diff.
  # Uses short hash + full subject + body (separated by blank lines) so agents
  # can see the author's intent. Capped at 20 commits and 4000 chars total.
  local _cl_range
  if [[ -n "$DIFF_BASE" ]]; then
    _cl_range="${DIFF_BASE}..${HEAD_SHA}"
  else
    _cl_range="origin/${BASE_REF}..${HEAD_SHA}"
  fi
  COMMIT_LOG=$(set +o pipefail; git log --format='%h %s%n%b' --max-count=20 "$_cl_range" 2>/dev/null \
    | head -c 4000) || true

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
    local lang_lower
    lang_lower=$(echo "$lang" | tr '[:upper:]' '[:lower:]')
    local profile="${SCRIPT_DIR}/language-profiles/${lang_lower}.md"
    if [[ -f "$profile" ]]; then
      LANGUAGE_CONTEXT+=$'\n'"$(cat "$profile")"$'\n'
    fi
  done

  # Read project context (CLAUDE.md) if available
  PROJECT_CONTEXT=""
  if [[ -f "CLAUDE.md" ]]; then
    PROJECT_CONTEXT=$(head -c 2000 CLAUDE.md)
    if [[ $(wc -c < CLAUDE.md) -gt 2000 ]]; then
      echo "NOTE: CLAUDE.md truncated to 2000 chars for agent context." >&2
    fi
  fi
}

# Computes conditional-agent dispatch flags from $DIFF_FILE and $CHANGED_FILES globals.
# Reads:  DIFF_FILE (full unified diff), CHANGED_FILES (newline-separated file paths)
# Sets four globals consumed by review.sh dispatch sites:
#   HAS_ERROR_PATTERNS         — 0|1; controls silent-failure-hunter
#   RUN_ARCHITECTURE_REVIEWER  — true|false; default true
#   RUN_SECURITY_REVIEWER      — true|false; default true
#   RUN_EDGE_CASE_HUNTER       — true|false; default true
#
# Conservative: each RUN_* defaults true. A gate only flips to false when
# (a) its kill switch is unset/false AND (b) the heuristic says skip.
# Kill switches (env-var-only, no action.yml input):
#   AI_DISABLE_GATE_ARCHITECTURE=true — disables the docs-only heuristic; architecture-reviewer always runs
#   AI_DISABLE_GATE_SECURITY=true     — disables the keyword/path heuristic; security-reviewer always runs
#   AI_DISABLE_GATE_EDGE_CASE=true    — disables the control-flow heuristic; edge-case-hunter always runs
#
# Note: gates evaluate against $DIFF_FILE, which for incremental reviews is the
# watermark-to-HEAD diff rather than the full base..HEAD PR diff. A trivial follow-up
# commit (e.g., docs tweak) can suppress Tier-2 agents even if the PR overall
# contains security-relevant code. Use AI_DISABLE_GATE_* to override when needed.
#
# TODO(#129): RUN_* globals and dispatch-site guards should be replaced by per-entry
# condition callbacks in the declarative agent roster when that feature lands.
# shellcheck disable=SC2034  # globals (HAS_ERROR_PATTERNS, RUN_*) are read by review.sh callers
detect_conditional_agent_triggers() {
  HAS_ERROR_PATTERNS=0
  if grep -qE '(catch|if err|try \{|rescue|Result<|unwrap|except|\.catch\()' "$DIFF_FILE" 2>/dev/null; then
    HAS_ERROR_PATTERNS=1
  fi

  RUN_ARCHITECTURE_REVIEWER=true
  RUN_SECURITY_REVIEWER=true
  RUN_EDGE_CASE_HUNTER=true

  # Architecture gate: skip when all changes are docs/meta only.
  # .github/workflows/ files are treated as infra (architectural); other .github/
  # paths (ISSUE_TEMPLATE, CODEOWNERS, etc.) are treated as docs.
  if [[ "${AI_DISABLE_GATE_ARCHITECTURE:-false}" != "true" ]] && [[ -n "$CHANGED_FILES" ]]; then
    local workflow_count nondocs_count
    workflow_count=$(printf '%s\n' "$CHANGED_FILES" | grep -cE '(^|/)\.github/workflows/' || true)
    nondocs_count=$(printf '%s\n' "$CHANGED_FILES" \
      | grep -vE '(^|/)\.github/workflows/' \
      | grep -vE '\.(md|markdown|txt|rst|adoc)$' \
      | grep -vE '(^|/)(docs|memory-bank|\.github|\.claude)/' \
      | grep -vE '(^|/)(CHANGELOG|README|LICENSE|NOTICE|AUTHORS|CONTRIBUTING|CODEOWNERS|CODE_OF_CONDUCT)(\..*)?$' \
      | grep -cv '^$' || true)
    if [[ "$workflow_count" -eq 0 ]] && [[ "$nondocs_count" -eq 0 ]]; then
      RUN_ARCHITECTURE_REVIEWER=false
    fi
  fi

  # Security gate: skip when diff contains no security-adjacent keywords AND no
  # security-sensitive file paths. Intentionally broad — false positives cost one
  # extra agent run; false negatives cost a missed vulnerability.
  if [[ "${AI_DISABLE_GATE_SECURITY:-false}" != "true" ]]; then
    local has_sec_keyword=0 has_sec_path=0
    if grep -qiE \
        'auth|token|secret|password|crypt|hash|\bsign\b|verify|exec|eval|sql|sanitize|escape|xss|csrf|cors|header|redirect|deserialize|cookie|session|jwt|oauth|ldap|saml|rbac|acl|permission|privilege|sudo|chmod|chown|setuid|x509|tls|ssl|cert|certificate|keystore|nonce|salt|hmac|aes|rsa|ecdsa|pbkdf2|bcrypt|scrypt|curl|wget|\bsource\b|\bIFS\b|LD_PRELOAD|\$\{\{' \
        "$DIFF_FILE" 2>/dev/null; then
      has_sec_keyword=1
    fi
    if [[ -n "$CHANGED_FILES" ]] && printf '%s\n' "$CHANGED_FILES" | grep -qE \
        -e '(auth|passwords?|credentials?|tokens?|secrets?)' \
        -e '(^|/)(api|routes?)/' \
        -e '(^|/)(package\.json|package-lock\.json|go\.mod|go\.sum|composer\.json|composer\.lock|requirements[^/]*\.txt|pyproject\.toml|Pipfile|Pipfile\.lock|Gemfile|Gemfile\.lock|[Cc]argo\.toml|[Cc]argo\.lock|yarn\.lock|pnpm-lock\.yaml)$' \
        -e '(^|/)\.env' \
        -e '(^|/)settings\.(py|ya?ml|json|toml)$' \
        -e '(^|/)Dockerfile' \
        -e '(^|/)Containerfile' \
        -e '\.(sh|bash)$' \
        -e '(^|/)\.github/workflows/' 2>/dev/null; then
      has_sec_path=1
    fi
    if [[ "$has_sec_keyword" -eq 0 ]] && [[ "$has_sec_path" -eq 0 ]]; then
      RUN_SECURITY_REVIEWER=false
    fi
  fi

  # Edge-case gate: skip when diff additions contain no control-flow keywords.
  # Checks only added lines (+ prefix) — removed control flow is not a concern.
  if [[ "${AI_DISABLE_GATE_EDGE_CASE:-false}" != "true" ]]; then
    if ! grep -E '^\+[^+]' "$DIFF_FILE" 2>/dev/null \
        | grep -qE '\b(if|elif|else|for|while|do|case|switch|match|try|catch|except|rescue|unless|when|loop|break|continue|return|goto|defer|finally)\b'; then
      RUN_EDGE_CASE_HUNTER=false
    fi
  fi
}
