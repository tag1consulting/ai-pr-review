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
