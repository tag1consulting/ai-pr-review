#!/usr/bin/env bash
#
# post-review-gitlab.sh — Post AI review results to a GitLab MR.
#
# Posts a summary note (comment) and inline findings as MR discussions with
# optional suggestion fences. Supports incremental review via SHA watermark,
# stale discussion resolution, and self-hosted GitLab instances.
#
# Usage:
#   ./post-review-gitlab.sh --get-last-sha <mr_number>
#   ./post-review-gitlab.sh <mr_number> <summary_file> <findings_file>
#                           <findings_json_file> <diff_file> <head_sha>
#                           [token_table_file]
#
# Environment (required):
#   GITLAB_TOKEN           — GitLab personal/project access token (api scope)
#                            Falls back to CI_JOB_TOKEN with JOB-TOKEN header
#   GITHUB_REPOSITORY      — reused as "namespace/project" (set by the
#                            CI wrapper from $CI_PROJECT_PATH)
#
# Environment (optional):
#   GITLAB_API_URL         — API base URL (default: https://gitlab.com/api/v4)
#   GITLAB_PROJECT_ID      — numeric project ID (overrides path-based lookup)
#   CI_PROJECT_ID          — fallback numeric project ID (set by GitLab CI)
#   CI_PROJECT_PATH        — fallback project path (set by GitLab CI)
#   GITLAB_MR_DIFF_BASE_SHA — base SHA for inline discussion positions
#                             (falls back to CI_MERGE_REQUEST_DIFF_BASE_SHA)
#   GITLAB_BOT_USERNAME    — username of the bot posting reviews (for stale
#                            thread resolution; defaults to authenticated user)
#   AI_REVIEW_FAILED_AGENTS — colon-separated list of failed agents (for
#                             the "incomplete review" notice in the body)
#   AI_ENABLE_SUGGESTIONS  — enable suggestion fences in inline discussions
#                            (default: true)
#   AI_MAX_INLINE          — max inline discussion comments (default: 25)
#
# Sibling of post-review.sh and post-review-bitbucket.sh. Pure helpers
# (truncate_body, severity_icon, format_source_tag, format_body_finding,
# build_agent_prompt, parse_valid_lines, parse_diff_new_lines,
# mktemp_tracked, cleanup) are duplicated here rather than sourced to keep
# the three scripts independent. Each duplicate is marked with a
# "# keep in sync with post-review.sh:<line>" comment.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve GitLab project identifier. Prefer explicit numeric ID; fall back
# to URL-encoding a project path.
# ---------------------------------------------------------------------------
resolve_project_id() {
  if [[ -n "${GITLAB_PROJECT_ID:-}" && "${GITLAB_PROJECT_ID}" =~ ^[0-9]+$ ]]; then
    PROJECT_ID="$GITLAB_PROJECT_ID"
  elif [[ -n "${CI_PROJECT_ID:-}" && "${CI_PROJECT_ID}" =~ ^[0-9]+$ ]]; then
    PROJECT_ID="$CI_PROJECT_ID"
  elif [[ -n "${CI_PROJECT_PATH:-}" ]]; then
    PROJECT_ID=$(printf '%s' "$CI_PROJECT_PATH" | sed 's|/|%2F|g')
  elif [[ -n "${GITHUB_REPOSITORY:-}" && "$GITHUB_REPOSITORY" == */* ]]; then
    if [[ ! "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]]; then
      echo "ERROR: GITHUB_REPOSITORY must be in 'namespace/project' format (got '${GITHUB_REPOSITORY}')." >&2
      exit 1
    fi
    PROJECT_ID=$(printf '%s' "$GITHUB_REPOSITORY" | sed 's|/|%2F|g')
  else
    echo "ERROR: Cannot resolve GitLab project ID. Set GITLAB_PROJECT_ID, CI_PROJECT_ID, CI_PROJECT_PATH, or GITHUB_REPOSITORY." >&2
    exit 1
  fi
}

GL_API="${GITLAB_API_URL:-https://gitlab.com/api/v4}"
MARKER_PREFIX="<!-- ai-pr-review-summary"

# ---------------------------------------------------------------------------
# Temp file bookkeeping (keep in sync with post-review.sh:322).
# ---------------------------------------------------------------------------
TMPFILES=()
cleanup() {
  rm -f "${TMPFILES[@]}" 2>/dev/null || true
}
trap cleanup EXIT

# keep in sync with post-review.sh:327
mktemp_tracked() {
  local f
  f=$(mktemp "$@")
  TMPFILES+=("$f")
  echo "$f"
}

# ---------------------------------------------------------------------------
# gl_api — invoke the GitLab REST API v4 with token auth.
# Mirrors the bb_api pattern: retries on transient failures (408, 429,
# 500, 502, 503, 504, and curl-level failures indicated by code 000) with
# exponential backoff + jitter. Prints response body to stdout on success;
# on failure, prints the body and returns non-zero.
#
# Usage: gl_api <method> <path_after_/v4> [curl_args...]
#   e.g. gl_api GET "/projects/123/merge_requests/1/notes"
#        gl_api POST "/projects/123/merge_requests/1/notes" \
#                    --data-urlencode "body=comment text"
# ---------------------------------------------------------------------------
gl_api() {
  local method="$1" path="$2"
  shift 2

  # Determine auth header: prefer GITLAB_TOKEN (PRIVATE-TOKEN), fall back
  # to CI_JOB_TOKEN (JOB-TOKEN) for limited-scope CI operations.
  local auth_header
  if [[ -n "${GITLAB_TOKEN:-}" ]]; then
    auth_header="PRIVATE-TOKEN: ${GITLAB_TOKEN}"
  elif [[ -n "${CI_JOB_TOKEN:-}" ]]; then
    auth_header="JOB-TOKEN: ${CI_JOB_TOKEN}"
  else
    echo "ERROR: GITLAB_TOKEN or CI_JOB_TOKEN is required for GitLab API auth." >&2
    return 1
  fi

  local attempt=0 max_retries=3 http_code body_file curl_err_file
  body_file=$(mktemp_tracked /tmp/gl-api-body-XXXXXXXX)
  curl_err_file=$(mktemp_tracked /tmp/gl-api-err-XXXXXXXX)

  while true; do
    http_code=$(curl -sS \
      -H "$auth_header" \
      --connect-timeout 15 \
      --max-time 60 \
      -o "$body_file" \
      -w '%{http_code}' \
      -X "$method" \
      "$@" \
      "${GL_API}${path}" 2>"$curl_err_file" || echo "000")

    if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
      cat "$body_file"
      return 0
    fi

    # Retry on transient: 408, 429, 500, 502, 503, 504, and curl failure (000).
    if [[ "$attempt" -lt "$max_retries" ]] && \
       [[ "$http_code" =~ ^(408|429|500|502|503|504|000)$ ]]; then
      attempt=$((attempt + 1))
      local backoff=$(( 2 * (1 << (attempt - 1)) ))
      local jitter; jitter=$(printf '%03d' $(( RANDOM % 1000 )))
      echo "WARNING: gl_api ${method} ${path} -> ${http_code} (attempt ${attempt}/${max_retries}), retrying in ${backoff}.${jitter}s..." >&2
      sleep "${backoff}.${jitter}"
      continue
    fi

    # Permanent failure or retries exhausted — emit full diagnostics.
    echo "ERROR: gl_api ${method} ${path} -> ${http_code}" >&2
    [[ -s "$curl_err_file" ]] && cat "$curl_err_file" >&2
    cat "$body_file" >&2
    return 1
  done
}

# ---------------------------------------------------------------------------
# --get-last-sha mode: must run before positional-arg validation so it can
# be invoked with only one argument. Returns the SHA via stdout, or empty
# string on first run. Never fails the caller on API error.
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--get-last-sha" ]]; then
  MR_NUMBER="${2:?--get-last-sha requires MR number as second argument}"
  resolve_project_id

  local_page=1
  comment_body=""
  while true; do
    notes_json=$(gl_api GET "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/notes?per_page=100&page=${local_page}&sort=desc&order_by=updated_at") || {
      echo "WARNING: get_last_reviewed_sha: GitLab API error (treating as first run)." >&2
      exit 0
    }

    comment_body=$(echo "$notes_json" | jq -r --arg marker "$MARKER_PREFIX" \
      '[.[] | select(.body // "" | contains($marker))] | first.body // empty' 2>/dev/null) || {
      echo "WARNING: get_last_reviewed_sha: could not parse notes response (treating as first run)." >&2
      exit 0
    }

    [[ -n "$comment_body" ]] && break

    # Check if there are more pages via array length
    local_count=$(echo "$notes_json" | jq 'length' 2>/dev/null || echo "0")
    [[ "$local_count" -lt 100 ]] && break
    local_page=$((local_page + 1))
  done

  if [[ -n "$comment_body" ]]; then
    echo "$comment_body" | grep -oE 'sha=[0-9a-f]+' | sed 's/sha=//' | head -1 || true
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Reject --standalone: not yet implemented for GitLab (planned).
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--standalone" ]]; then
  echo "ERROR: Standalone review mode is not yet supported for GitLab (planned for a future release)." >&2
  echo "Use REVIEW_TARGET=pr instead." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Main mode: positional args.
# ---------------------------------------------------------------------------
MR_NUMBER="${1:?Missing MR number}"
SUMMARY_FILE="${2:?Missing summary file}"
# shellcheck disable=SC2034
FINDINGS_FILE="${3:?Missing findings file}"
FINDINGS_JSON_FILE="${4:?Missing findings JSON file}"
DIFF_FILE="${5:?Missing diff file}"
HEAD_SHA="${6:?Missing head SHA}"
TOKEN_TABLE_FILE="${7:-}"

# Validate HEAD_SHA is a hex git SHA before it is interpolated into sed
# expressions or embedded in the SHA watermark marker.
if [[ ! "$HEAD_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "ERROR: HEAD_SHA must be a hex git SHA (got '${HEAD_SHA}')." >&2
  exit 1
fi

resolve_project_id

# ---------------------------------------------------------------------------
# GitLab MR notes have a ~1,000,000 char limit but posting enormous comments
# is poor UX. Truncate at 250,000 to stay well within the limit while being
# generous enough to never truncate real reviews.
# keep in sync with post-review.sh:337 (different cap; GitHub uses 64,000)
# ---------------------------------------------------------------------------
MAX_BODY_SIZE=250000
truncate_body() {
  local body="$1" byte_len
  byte_len=$(printf '%s' "$body" | wc -c)
  if [[ "$byte_len" -gt "$MAX_BODY_SIZE" ]]; then
    local truncated
    local raw_cut
    raw_cut=$(printf '%s' "$body" | head -c "$MAX_BODY_SIZE")
    if command -v iconv > /dev/null 2>&1; then
      truncated=$(printf '%s' "$raw_cut" | iconv -f UTF-8 -t UTF-8//IGNORE 2>/dev/null)
      [[ -z "$truncated" ]] && truncated="$raw_cut"
    else
      truncated="$raw_cut"
    fi
    printf '%s\n\n---\n*Review output truncated — body exceeded GitLab MR note size limit. Run a full review locally to see complete output.*\n' \
      "$truncated"
  else
    printf '%s' "$body"
  fi
}

# keep in sync with post-review.sh:735
severity_icon() {
  case "${1,,}" in
    critical) echo "❌" ;;
    high)     echo "🚨" ;;
    medium)   echo "🔶" ;;
    low)      echo "💬" ;;
    *)        echo "⚪" ;;
  esac
}

# keep in sync with post-review.sh:748
format_source_tag() {
  local finding_obj="$1"
  local sources primary rest
  sources=$(echo "$finding_obj" | jq -r '
    if (.sources | type) == "array" and (.sources | length) > 0 then
      .sources[]
    else
      (.source // "unknown")
    end
  ' 2>/dev/null)

  primary=$(echo "$sources" | head -1)
  [[ -z "$primary" ]] && { echo "[unknown]"; return; }
  rest=$(echo "$sources" | tail -n +2 | paste -sd ', ' -)

  if [[ -n "$rest" ]]; then
    echo "[${primary}] *(also flagged by: ${rest})*"
  else
    echo "[${primary}]"
  fi
}

# ---------------------------------------------------------------------------
# keep in sync with post-review.sh:662
# Parse diff to emit file:line pairs for added (+) lines only.
# These are valid targets for inline MR discussions.
# ---------------------------------------------------------------------------
parse_valid_lines() {
  local diff_file="$1"
  local current_file=""
  local new_line=0

  while IFS= read -r line; do
    if [[ "$line" =~ ^diff\ --git\ a/(.+)\ b/(.+) ]]; then
      current_file="${BASH_REMATCH[2]}"
      new_line=0
    elif [[ "$line" =~ ^\+\+\+\  || "$line" =~ ^---\  ]]; then
      continue
    elif [[ "$line" =~ ^@@\ -[0-9]+(,[0-9]+)?\ \+([0-9]+)(,[0-9]+)?\ @@ ]]; then
      new_line="${BASH_REMATCH[2]}"
    elif [[ -n "$current_file" && "$new_line" -gt 0 ]]; then
      if [[ "$line" =~ ^\+ ]]; then
        echo "${current_file}:${new_line}"
        new_line=$((new_line + 1))
      elif [[ "$line" =~ ^- ]]; then
        :
      elif [[ "$line" =~ ^\\ ]]; then
        :
      else
        new_line=$((new_line + 1))
      fi
    fi
  done < "$diff_file"
}

# ---------------------------------------------------------------------------
# keep in sync with post-review.sh:701
# Parse diff to emit file:line pairs for both added and context lines
# (NOT deleted lines). Used to validate multi-line suggestion ranges.
# ---------------------------------------------------------------------------
parse_diff_new_lines() {
  local diff_file="$1"
  local current_file=""
  local new_line=0

  while IFS= read -r line; do
    if [[ "$line" =~ ^diff\ --git\ a/(.+)\ b/(.+) ]]; then
      current_file="${BASH_REMATCH[2]}"
      new_line=0
    elif [[ "$line" =~ ^\+\+\+\  || "$line" =~ ^---\  ]]; then
      continue
    elif [[ "$line" =~ ^@@\ -[0-9]+(,[0-9]+)?\ \+([0-9]+)(,[0-9]+)?\ @@ ]]; then
      new_line="${BASH_REMATCH[2]}"
    elif [[ -n "$current_file" && "$new_line" -gt 0 ]]; then
      if [[ "$line" =~ ^\+ ]]; then
        echo "${current_file}:${new_line}"
        new_line=$((new_line + 1))
      elif [[ "$line" =~ ^- ]]; then
        :
      elif [[ "$line" =~ ^\\ ]]; then
        :
      else
        echo "${current_file}:${new_line}"
        new_line=$((new_line + 1))
      fi
    fi
  done < "$diff_file"
}

# ---------------------------------------------------------------------------
# keep in sync with post-review.sh:779
# Format a single finding for the review body (non-inline).
# Includes remediation and suggested_code in a collapsible <details> block
# when present. suggested_code renders as a plain code fence (not a
# suggestion fence, which only works in inline MR discussions).
# Args: severity source_tag finding location loc_note remediation [suggested_code]
# ---------------------------------------------------------------------------
format_body_finding() {
  local severity="$1" source_tag="$2" finding="$3" location="$4" loc_note="$5" remediation="$6"
  local suggested_code="${7:-}"
  local bullet
  bullet="- $(severity_icon "$severity") **[${severity}]** ${source_tag} ${finding} — \`${location}\`${loc_note}"
  if [[ -n "$remediation" || -n "$suggested_code" ]]; then
    local details=""
    if [[ -n "$remediation" ]]; then
      details="**Remediation:** ${remediation}"
    fi
    if [[ -n "$suggested_code" && "$suggested_code" != *'```'* ]]; then
      local indented_code
      indented_code=$(printf '%s' "$suggested_code" | sed 's/^/  /')
      if [[ -n "$details" ]]; then
        details="${details}

  "
      fi
      details="${details}**Suggested fix:**
  \`\`\`
${indented_code}
  \`\`\`"
    fi
    printf '%s\n  <details>\n  <summary>Details</summary>\n\n  %s\n\n  </details>' "$bullet" "$details"
  else
    printf '%s' "$bullet"
  fi
}

# ---------------------------------------------------------------------------
# keep in sync with post-review.sh:815
# Build a collapsible "Prompt for AI agents" block from the findings JSON.
# ---------------------------------------------------------------------------
build_agent_prompt() {
  local findings_json="$1"
  local count
  count=$(echo "$findings_json" | jq 'length' 2>/dev/null || echo 0)
  [[ "$count" -eq 0 ]] && return

  local prompt_body
  prompt_body=$(echo "$findings_json" | jq -r '
    group_by(.file) | map(
      "In `\(.[0].file)`:" as $header |
      [$header] + [
        .[] |
        "- Around line \(.line // "?"): \(.finding)" +
          if (.remediation // "") != "" then ". " + .remediation else "" end
      ] | join("\n")
    ) | join("\n\n")
  ' 2>/dev/null)

  [[ -z "$prompt_body" ]] && return

  printf '<details>\n<summary>🤖 Prompt for AI agents</summary>\n\n```\nVerify each finding against the current code and only fix it if needed.\n\n%s\n```\n\n</details>' "$prompt_body"
}

# ---------------------------------------------------------------------------
# Classify overall risk from the findings JSON. Prints "<risk>|<event>".
# keep in sync with post-review-bitbucket.sh:349
# ---------------------------------------------------------------------------
classify_risk() {
  local findings_json="$1"
  local finding_total failed_agents_env
  finding_total=$(echo "$findings_json" | jq 'length')
  failed_agents_env="${AI_REVIEW_FAILED_AGENTS:-}"

  if [[ "$finding_total" -eq 0 ]]; then
    if [[ -n "$failed_agents_env" ]]; then
      echo "Unknown|COMMENT"
    else
      echo "None|APPROVE"
    fi
  elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "critical")' > /dev/null 2>&1; then
    echo "Critical|REQUEST_CHANGES"
  elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "high")' > /dev/null 2>&1; then
    echo "High|REQUEST_CHANGES"
  elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "medium")' > /dev/null 2>&1; then
    echo "Medium|APPROVE"
  else
    echo "Low|APPROVE"
  fi
}

# ---------------------------------------------------------------------------
# Build the complete summary comment body: marker + summary + body findings
# + tokens + agent prompt.
# Args: body_findings (pre-rendered markdown for overflow findings)
#       inline_count (number of findings posted inline)
# ---------------------------------------------------------------------------
build_comment_body() {
  local body_findings="${1:-}" inline_count="${2:-0}"
  local summary findings_json

  if [[ ! -f "$SUMMARY_FILE" ]]; then
    echo "ERROR: Summary file not found: ${SUMMARY_FILE}. This indicates an upstream agent failure." >&2
    return 1
  fi
  summary=$(cat "$SUMMARY_FILE")

  findings_json="[]"
  if [[ -f "$FINDINGS_JSON_FILE" ]]; then
    findings_json=$(cat "$FINDINGS_JSON_FILE")
    if ! echo "$findings_json" | jq -e 'type == "array"' > /dev/null 2>&1; then
      echo "ERROR: Findings JSON file is not a valid JSON array. Review results may be incomplete." >&2
      return 1
    fi
  fi

  local risk_event risk event finding_total
  risk_event=$(classify_risk "$findings_json")
  risk="${risk_event%|*}"
  event="${risk_event#*|}"
  finding_total=$(echo "$findings_json" | jq 'length')

  local failed_agents_env="${AI_REVIEW_FAILED_AGENTS:-}"
  local heading summary_block findings_block=""

  if [[ "$event" == "APPROVE" && "$finding_total" -eq 0 ]]; then
    heading="## AI Review: Approved"
    summary_block="No findings above the confidence threshold. The changes look good."
  elif [[ "$event" == "COMMENT" && "$risk" == "Unknown" ]]; then
    heading="## AI Review: Incomplete"
    summary_block="No findings above the confidence threshold, but one or more agents failed: ${failed_agents_env//:/, }

The review may be incomplete. Please verify manually or re-run the review."
  elif [[ "$event" == "APPROVE" ]]; then
    heading="## AI Review: Approved"
    summary_block="$(severity_icon "$risk") **Overall Risk:** ${risk} | **Findings:** ${finding_total} (${inline_count} inline)

No Critical or High findings. The changes look good — Medium/Low findings are informational only."
    if [[ -n "$body_findings" ]]; then
      findings_block="### Findings (informational)
${body_findings}"
    fi
  else
    heading="## AI Review Findings"
    summary_block="$(severity_icon "$risk") **Overall Risk:** ${risk} | **Findings:** ${finding_total} (${inline_count} inline)"
    if [[ -n "$body_findings" ]]; then
      findings_block="### Findings not attached to specific lines
${body_findings}"
    elif [[ "$inline_count" -gt 0 ]]; then
      findings_block="All findings are attached as inline comments."
    fi
  fi

  local sha_marker="${MARKER_PREFIX} sha=${HEAD_SHA} -->"

  local pr_summary_block=""
  if [[ -n "$summary" ]]; then
    pr_summary_block="
### Summary
${summary}
"
  fi

  local token_block=""
  if [[ -n "$TOKEN_TABLE_FILE" && -s "$TOKEN_TABLE_FILE" ]]; then
    token_block="
$(cat "$TOKEN_TABLE_FILE")
"
  fi

  local body
  body="${sha_marker}
${heading}

${summary_block}
${pr_summary_block}
${findings_block}
${token_block}"

  # Append agent prompt block when findings exist (GitLab renders <details>)
  if [[ "$finding_total" -gt 0 ]]; then
    local agent_prompt
    agent_prompt=$(build_agent_prompt "$findings_json")
    if [[ -n "$agent_prompt" ]]; then
      body="${body}

${agent_prompt}"
    fi
  fi

  body="${body}

---
*AI Review — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"

  truncate_body "$body"
}

# ---------------------------------------------------------------------------
# Find an existing summary comment by marker. Returns the note id on
# stdout, or empty if none exists.
# ---------------------------------------------------------------------------
find_existing_summary_id() {
  local page=1
  while true; do
    local notes_json
    notes_json=$(gl_api GET "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/notes?per_page=100&page=${page}&sort=desc&order_by=updated_at") || return 1

    local found_id
    found_id=$(echo "$notes_json" | jq -r --arg marker "$MARKER_PREFIX" \
      '[.[] | select((.body // "") | contains($marker))] | first.id // empty' 2>/dev/null) || {
      echo "WARNING: find_existing_summary_id: could not parse notes response." >&2
      return 1
    }

    [[ -n "$found_id" ]] && { echo "$found_id"; return 0; }

    local count
    count=$(echo "$notes_json" | jq 'length' 2>/dev/null || echo "0")
    [[ "$count" -lt 100 ]] && break
    page=$((page + 1))
  done
  echo ""
}

# ---------------------------------------------------------------------------
# Upsert the summary comment. POST on first run, PUT on subsequent runs.
# ---------------------------------------------------------------------------
post_summary_with_findings() {
  local body="$1"

  local existing_id
  if ! existing_id=$(find_existing_summary_id); then
    echo "ERROR: Could not query existing notes; cannot safely upsert." >&2
    return 1
  fi

  local result
  if [[ -n "$existing_id" ]]; then
    echo "Updating existing summary note #${existing_id}..." >&2
    if result=$(gl_api PUT \
      "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/notes/${existing_id}" \
      --data-urlencode "body=${body}"); then
      echo "Summary note updated on MR !${MR_NUMBER}." >&2
      _cleanup_duplicate_summary_comments "$existing_id" || true
      return 0
    fi
    echo "ERROR: Failed to update summary note #${existing_id}." >&2
    return 1
  fi

  echo "Posting new summary note..." >&2
  if result=$(gl_api POST \
    "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/notes" \
    --data-urlencode "body=${body}"); then
    local new_id
    new_id=$(echo "$result" | jq -r '.id // empty' || true)
    echo "Summary note posted to MR !${MR_NUMBER} (id=${new_id:-unknown})." >&2
    _cleanup_duplicate_summary_comments "${new_id:-}" || true
    return 0
  fi
  echo "ERROR: Failed to post summary note to MR !${MR_NUMBER}. Review results were not delivered." >&2
  return 1
}

# Delete all summary-marker notes except the one we just kept. Non-fatal.
_cleanup_duplicate_summary_comments() {
  local kept_id="$1"
  [[ -z "$kept_id" ]] && return 0

  local page=1
  while true; do
    local notes_json
    notes_json=$(gl_api GET "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/notes?per_page=100&page=${page}&sort=desc&order_by=updated_at") || break

    while IFS= read -r dup_id; do
      [[ -z "$dup_id" || "$dup_id" == "$kept_id" ]] && continue
      echo "Removing duplicate summary note #${dup_id}..." >&2
      gl_api DELETE \
        "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/notes/${dup_id}" \
        > /dev/null \
        || echo "WARNING: Could not delete duplicate summary note #${dup_id}." >&2
    done < <(echo "$notes_json" | jq -r --arg marker "$MARKER_PREFIX" \
      '[.[] | select((.body // "") | contains($marker))] | .[].id' 2>/dev/null)

    local count
    count=$(echo "$notes_json" | jq 'length' 2>/dev/null || echo "0")
    [[ "$count" -lt 100 ]] && break
    page=$((page + 1))
  done
}

# ---------------------------------------------------------------------------
# Resolve stale bot-authored MR discussions.
# Fetches all discussion threads, finds those authored by the bot user that
# are unresolved, and resolves them via PUT.
# ---------------------------------------------------------------------------
resolve_stale_discussions() {
  echo "Resolving stale MR discussions..." >&2

  # Determine the bot username for filtering. If GITLAB_BOT_USERNAME is not
  # set, fetch the authenticated user's username.
  local bot_username="${GITLAB_BOT_USERNAME:-}"
  if [[ -z "$bot_username" ]]; then
    bot_username=$(gl_api GET "/user" | jq -r '.username // empty' 2>/dev/null) || {
      echo "WARNING: Could not determine authenticated user; skipping stale discussion resolution." >&2
      return 0
    }
    if [[ -z "$bot_username" ]]; then
      echo "WARNING: Could not determine bot username; skipping stale discussion resolution." >&2
      return 0
    fi
  fi

  local page=1 resolved=0 failed=0
  while true; do
    local discussions_json
    discussions_json=$(gl_api GET "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/discussions?per_page=100&page=${page}") || {
      echo "WARNING: Could not fetch MR discussions for resolution." >&2
      return 0
    }

    local discussion_ids
    discussion_ids=$(echo "$discussions_json" | jq -r --arg bot "$bot_username" '
      .[] | select(
        (.notes[0].author.username // "") == $bot and
        (.notes[0].resolvable // false) == true and
        (.notes[0].resolved // false) == false
      ) | .id
    ' 2>/dev/null) || true

    while IFS= read -r disc_id; do
      [[ -z "$disc_id" ]] && continue
      if gl_api PUT "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/discussions/${disc_id}?resolved=true" > /dev/null 2>&1; then
        resolved=$((resolved + 1))
      else
        echo "WARNING: Could not resolve discussion ${disc_id}." >&2
        failed=$((failed + 1))
      fi
    done <<< "$discussion_ids"

    local count
    count=$(echo "$discussions_json" | jq 'length' 2>/dev/null || echo "0")
    [[ "$count" -lt 100 ]] && break
    page=$((page + 1))
  done

  if [[ "$resolved" -gt 0 || "$failed" -gt 0 ]]; then
    if [[ "$failed" -gt 0 ]]; then
      echo "Resolved ${resolved} stale discussion(s); ${failed} failed to resolve." >&2
    else
      echo "Resolved ${resolved} stale discussion(s)." >&2
    fi
  else
    echo "No stale discussions to resolve." >&2
  fi
}

# ---------------------------------------------------------------------------
# Post inline findings as individual MR discussions with optional suggestion
# fences. Populates body_findings_out (via nameref) with overflow findings
# that could not be posted inline.
#
# Returns: inline_count via stdout
# ---------------------------------------------------------------------------
post_inline_discussions() {
  local findings_json="[]"
  if [[ -f "$FINDINGS_JSON_FILE" ]]; then
    findings_json=$(cat "$FINDINGS_JSON_FILE")
    if ! echo "$findings_json" | jq -e 'type == "array"' > /dev/null 2>&1; then
      echo "WARNING: Invalid findings JSON, skipping inline discussions." >&2
      echo "0"
      return 0
    fi
  fi

  local finding_total
  finding_total=$(echo "$findings_json" | jq 'length')
  if [[ "$finding_total" -eq 0 ]]; then
    echo "0"
    return 0
  fi

  # Resolve the diff base SHA for position objects
  local diff_base_sha="${GITLAB_MR_DIFF_BASE_SHA:-${CI_MERGE_REQUEST_DIFF_BASE_SHA:-}}"
  if [[ -z "$diff_base_sha" ]]; then
    echo "WARNING: No diff base SHA available (GITLAB_MR_DIFF_BASE_SHA / CI_MERGE_REQUEST_DIFF_BASE_SHA); skipping inline discussions." >&2
    # Return all findings as body findings
    local all_body=""
    local findings_ndjson
    findings_ndjson=$(echo "$findings_json" | jq -c '.[]' 2>/dev/null || true)
    while IFS= read -r finding_obj; do
      [[ -z "$finding_obj" ]] && continue
      local file line severity finding remediation source_tag
      file=$(echo "$finding_obj" | jq -r '.file // empty')
      line=$(echo "$finding_obj" | jq -r '.line // empty')
      severity=$(echo "$finding_obj" | jq -r '.severity // "Medium"')
      finding=$(echo "$finding_obj" | jq -r '.finding // empty')
      remediation=$(echo "$finding_obj" | jq -r '.remediation // empty')
      source_tag=$(format_source_tag "$finding_obj")
      [[ -z "$finding" ]] && continue
      all_body="${all_body}
$(format_body_finding "$severity" "$source_tag" "$finding" "${file:-unknown}:${line:-?}" "" "$remediation")"
    done <<< "$findings_ndjson"
    # Write body findings to a temp file for the caller to read
    if [[ -n "$all_body" ]]; then
      local bf_file
      bf_file=$(mktemp_tracked /tmp/gl-body-findings-XXXXXXXX)
      printf '%s' "$all_body" > "$bf_file"
      echo "BODY_FINDINGS_FILE=$bf_file" >&2
    fi
    echo "0"
    return 0
  fi

  # Build valid lines lookup from diff
  local valid_lines_file
  valid_lines_file=$(mktemp_tracked /tmp/gl-valid-lines-XXXXXXXX.txt)
  parse_valid_lines "$DIFF_FILE" > "$valid_lines_file"

  # When suggestions are enabled, build a lookup of all new-file lines
  local diff_lines_file=""
  local _enable_for_lookup="${AI_ENABLE_SUGGESTIONS:-true}"
  _enable_for_lookup="${_enable_for_lookup,,}"
  if [[ "$_enable_for_lookup" == "true" ]]; then
    diff_lines_file=$(mktemp_tracked /tmp/gl-diff-new-lines-XXXXXXXX.txt)
    parse_diff_new_lines "$DIFF_FILE" > "$diff_lines_file"
  fi

  local inline_count=0
  local body_findings=""
  local max_inline
  local _raw_mi="${AI_MAX_INLINE:-25}"
  if [[ "$_raw_mi" =~ ^[0-9]+$ ]]; then
    max_inline="$_raw_mi"
  else
    echo "WARNING: AI_MAX_INLINE='${_raw_mi}' is invalid; using default 25." >&2
    max_inline=25
  fi

  local findings_ndjson
  findings_ndjson=$(echo "$findings_json" | jq -c '.[]' 2>/dev/null || true)

  while IFS= read -r finding_obj; do
    [[ -z "$finding_obj" ]] && continue
    local file line severity finding remediation source_tag suggested_code start_line
    file=$(echo "$finding_obj" | jq -r '.file // empty')
    line=$(echo "$finding_obj" | jq -r '.line // empty')
    severity=$(echo "$finding_obj" | jq -r '.severity // "Medium"')
    finding=$(echo "$finding_obj" | jq -r '.finding // empty')
    remediation=$(echo "$finding_obj" | jq -r '.remediation // empty')
    suggested_code=$(echo "$finding_obj" | jq -r '.suggested_code // empty')
    start_line=$(echo "$finding_obj" | jq -r '.start_line // empty')
    source_tag=$(format_source_tag "$finding_obj")

    if [[ -z "$file" || -z "$line" || -z "$finding" ]]; then
      continue
    fi

    # Validate line is a positive integer
    if ! [[ "$line" =~ ^[0-9]+$ ]]; then
      echo "WARNING: Skipping finding with non-numeric line: ${file}:${line}" >&2
      body_findings="${body_findings}
$(format_body_finding "$severity" "$source_tag" "$finding" "${file}:${line}" "" "$remediation")"
      continue
    fi

    # Suggestion handling — gated on AI_ENABLE_SUGGESTIONS (case-insensitive)
    local _enable_suggestions_lc="${AI_ENABLE_SUGGESTIONS:-true}"
    _enable_suggestions_lc="${_enable_suggestions_lc,,}"
    if [[ "$_enable_suggestions_lc" != "true" ]]; then
      suggested_code=""
      start_line=""
    fi

    # Validate start_line: must be a positive integer (no leading zeros) and <= line
    if [[ -n "$start_line" ]]; then
      if ! [[ "$start_line" =~ ^[1-9][0-9]*$ ]] || [[ "$start_line" -gt "$line" ]]; then
        echo "WARNING: Invalid start_line='${start_line}' for ${file}:${line}; dropping suggestion." >&2
        start_line=""
        suggested_code=""
      fi
    fi

    # Cap multi-line suggestion ranges
    local MAX_SUGGESTION_RANGE=100
    if [[ -n "$suggested_code" && -n "$start_line" && "$start_line" != "$line" ]]; then
      if (( line - start_line + 1 > MAX_SUGGESTION_RANGE )); then
        echo "WARNING: Suggestion range ${file}:${start_line}-${line} exceeds max ${MAX_SUGGESTION_RANGE} lines; dropping suggestion." >&2
        suggested_code=""
        start_line=""
      fi
    fi

    # Reject suggested_code containing triple-backticks (fence escape prevention)
    if [[ -n "$suggested_code" && "$suggested_code" == *'```'* ]]; then
      echo "WARNING: suggested_code for ${file}:${line} contains triple-backticks; dropping suggestion to prevent fence escape." >&2
      suggested_code=""
      start_line=""
    fi

    # Validate multi-line suggestion range against diff
    if [[ -n "$suggested_code" && -n "$start_line" && "$start_line" != "$line" ]]; then
      if [[ -z "$diff_lines_file" ]]; then
        echo "WARNING: diff_lines_file unset for ${file}:${line} multi-line suggestion; dropping suggestion." >&2
        suggested_code=""
        start_line=""
      else
        local range_valid=true check_line
        for (( check_line=start_line; check_line<=line; check_line++ )); do
          if ! grep -qxF "${file}:${check_line}" "$diff_lines_file"; then
            range_valid=false
            break
          fi
        done
        if [[ "$range_valid" != "true" ]]; then
          echo "WARNING: Suggestion range ${file}:${start_line}-${line} not fully in diff; dropping suggestion." >&2
          suggested_code=""
          start_line=""
        fi
      fi
    fi

    # Check if this line is a valid inline target
    if grep -qxF "${file}:${line}" "$valid_lines_file" && [[ "$inline_count" -lt "$max_inline" ]]; then
      # Build the discussion comment body
      local comment_body
      comment_body="$(severity_icon "$severity") **[${severity}]** ${source_tag} ${finding}"
      if [[ -n "$remediation" ]]; then
        comment_body="${comment_body}

**Remediation:** ${remediation}"
      fi

      # Append suggestion fence in GitLab syntax
      if [[ -n "$suggested_code" ]]; then
        if [[ -n "$start_line" && "$start_line" != "$line" ]]; then
          local lines_above=$(( line - start_line ))
          comment_body="${comment_body}

\`\`\`suggestion:-${lines_above}+0
${suggested_code}
\`\`\`"
        else
          comment_body="${comment_body}

\`\`\`suggestion:-0+0
${suggested_code}
\`\`\`"
        fi
      fi

      # Build the position JSON and post the discussion
      local position_json
      position_json=$(jq -n \
        --arg base_sha "$diff_base_sha" \
        --arg start_sha "$diff_base_sha" \
        --arg head_sha "$HEAD_SHA" \
        --arg new_path "$file" \
        --arg old_path "$file" \
        --argjson new_line "$line" \
        '{
          position_type: "text",
          base_sha: $base_sha,
          start_sha: $start_sha,
          head_sha: $head_sha,
          new_path: $new_path,
          old_path: $old_path,
          new_line: $new_line
        }')

      local payload_file
      payload_file=$(mktemp_tracked /tmp/gl-disc-XXXXXXXX.json)
      jq -n \
        --arg body "$comment_body" \
        --argjson position "$position_json" \
        '{body: $body, position: $position}' > "$payload_file"

      local disc_result
      if disc_result=$(gl_api POST \
        "/projects/${PROJECT_ID}/merge_requests/${MR_NUMBER}/discussions" \
        -H 'Content-Type: application/json' \
        --data-binary "@${payload_file}" 2>&1); then
        inline_count=$((inline_count + 1))
      else
        # 400 = position invalid (line not in MR diff); fall back to body
        echo "WARNING: Failed to post inline discussion for ${file}:${line}: ${disc_result}" >&2
        local loc_note=" *(inline post failed)*"
        if [[ -n "$suggested_code" ]]; then
          echo "WARNING: Suggestion for ${file}:${line} not rendered inline (post failed); rendering in review body." >&2
        fi
        body_findings="${body_findings}
$(format_body_finding "$severity" "$source_tag" "$finding" "${file}:${line}" "$loc_note" "$remediation" "$suggested_code")"
      fi
    else
      # Append to body findings
      local loc_note=""
      local drop_reason=""
      if ! grep -qxF "${file}:${line}" "$valid_lines_file"; then
        loc_note=" *(line not in diff)*"
        drop_reason="line not in diff"
      else
        drop_reason="inline cap of ${max_inline} reached"
      fi
      if [[ -n "$suggested_code" ]]; then
        echo "WARNING: Suggestion for ${file}:${line} not rendered inline (${drop_reason}); rendering as code fence in review body instead." >&2
      fi
      body_findings="${body_findings}
$(format_body_finding "$severity" "$source_tag" "$finding" "${file}:${line}" "$loc_note" "$remediation" "$suggested_code")"
    fi
  done <<< "$findings_ndjson"

  # Write body findings to a temp file for the caller to read
  if [[ -n "$body_findings" ]]; then
    local bf_file
    bf_file=$(mktemp_tracked /tmp/gl-body-findings-XXXXXXXX)
    printf '%s' "$body_findings" > "$bf_file"
    echo "BODY_FINDINGS_FILE=$bf_file" >&2
  fi

  echo "$inline_count"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "--- Posting review to GitLab MR !${MR_NUMBER} ---" >&2

# Step 1: Resolve stale discussions from prior runs
resolve_stale_discussions

# Step 2: Post inline discussions and collect overflow body findings
# post_inline_discussions writes the inline count to stdout and
# BODY_FINDINGS_FILE=<path> to stderr if there are overflow findings.
inline_stderr_file=$(mktemp_tracked /tmp/gl-inline-stderr-XXXXXXXX)
inline_count=$(post_inline_discussions 2> >(tee "$inline_stderr_file" >&2))

body_findings=""
bf_path=$(grep -oP 'BODY_FINDINGS_FILE=\K.*' "$inline_stderr_file" 2>/dev/null || true)
if [[ -n "$bf_path" && -f "$bf_path" ]]; then
  body_findings=$(cat "$bf_path")
fi

echo "Posted ${inline_count} inline discussion(s)." >&2

# Step 3: Post (or update) the summary note with body findings
summary_body=$(build_comment_body "$body_findings" "$inline_count") || {
  echo "ERROR: Failed to build summary comment body." >&2
  exit 1
}

if ! post_summary_with_findings "$summary_body"; then
  echo "ERROR: Review results were not delivered to MR !${MR_NUMBER}." >&2
  exit 1
fi
