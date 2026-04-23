#!/usr/bin/env bash
#
# post-review-bitbucket.sh — Post AI review results to a Bitbucket Cloud PR.
#
# v0.2.0 scope: summary comment upsert + SHA watermark only. No inline
# comments (deferred to v0.3.0), no Code Insights, no standalone mode,
# no APPROVE/REQUEST_CHANGES events. Findings are rendered as markdown
# bullets inside the summary comment body.
#
# Usage:
#   ./post-review-bitbucket.sh --get-last-sha <pr_number>
#   ./post-review-bitbucket.sh <pr_number> <summary_file> <findings_file>
#                              <findings_json_file> <diff_file> <head_sha>
#                              [token_table_file]
#
# Environment (required):
#   BITBUCKET_EMAIL        — Atlassian account email (Basic auth username)
#   BITBUCKET_API_TOKEN    — Atlassian API token (Basic auth password)
#   GITHUB_REPOSITORY      — reused as "workspace/repo_slug" (set by the
#                            Pipelines wrapper from $BITBUCKET_WORKSPACE/
#                            $BITBUCKET_REPO_SLUG)
#
# Environment (optional):
#   BITBUCKET_WORKSPACE    — if set, overrides workspace from GITHUB_REPOSITORY
#   BITBUCKET_REPO_SLUG    — if set, overrides repo slug from GITHUB_REPOSITORY
#   AI_REVIEW_FAILED_AGENTS — colon-separated list of failed agents (for
#                             the "incomplete review" notice in the body)
#
# Sibling of post-review.sh. Pure helpers (truncate_body, severity_icon,
# format_source_tag, mktemp_tracked, cleanup) are duplicated here rather
# than sourced to keep the two scripts independent. Each duplicate is
# marked with a "# keep in sync with post-review.sh:<line>" comment.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve workspace + repo. Prefer explicit BITBUCKET_* vars; fall back to
# splitting GITHUB_REPOSITORY ("workspace/repo_slug").
# ---------------------------------------------------------------------------
resolve_repo_id() {
  if [[ -n "${BITBUCKET_WORKSPACE:-}" && -n "${BITBUCKET_REPO_SLUG:-}" ]]; then
    WORKSPACE="$BITBUCKET_WORKSPACE"
    REPO_SLUG="$BITBUCKET_REPO_SLUG"
  elif [[ -n "${GITHUB_REPOSITORY:-}" && "$GITHUB_REPOSITORY" == */* ]]; then
    # Validate exactly one slash to catch misconfigured values like "ws/proj/repo"
    # where ##*/ would silently drop the middle segment.
    if [[ ! "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]]; then
      echo "ERROR: GITHUB_REPOSITORY must be in 'workspace/repo_slug' format (got '${GITHUB_REPOSITORY}')." >&2
      exit 1
    fi
    WORKSPACE="${GITHUB_REPOSITORY%%/*}"
    REPO_SLUG="${GITHUB_REPOSITORY##*/}"
  else
    echo "ERROR: Cannot resolve Bitbucket workspace/repo. Set BITBUCKET_WORKSPACE and BITBUCKET_REPO_SLUG, or set GITHUB_REPOSITORY to 'workspace/repo_slug'." >&2
    exit 1
  fi
}

BB_API="https://api.bitbucket.org/2.0"
MARKER_PREFIX="<!-- ai-pr-review-summary"

# ---------------------------------------------------------------------------
# Temp file bookkeeping (keep in sync with post-review.sh:282).
# Defined here (ahead of bb_api) so bb_api can use mktemp_tracked rather than
# a RETURN trap that would not fire on SIGKILL or in some subshell contexts.
# ---------------------------------------------------------------------------
TMPFILES=()
cleanup() {
  rm -f "${TMPFILES[@]}" 2>/dev/null || true
}
trap cleanup EXIT

# keep in sync with post-review.sh:291
mktemp_tracked() {
  local f
  f=$(mktemp "$@")
  TMPFILES+=("$f")
  echo "$f"
}

# ---------------------------------------------------------------------------
# bb_api — invoke the Bitbucket Cloud REST API with Basic auth.
# Mirrors the gh_api_retry pattern: retries on transient failures (502/503/
# 429/timeouts) with exponential backoff + jitter. Prints response body to
# stdout on success; on failure, prints the body and returns non-zero.
#
# Usage: bb_api <method> <path_after_/2.0> [curl_args...]
#   e.g. bb_api GET "/repositories/ws/repo/pullrequests/1/comments"
#        bb_api POST "/repositories/ws/repo/pullrequests/1/comments" \
#                    --data-binary @body.json -H 'Content-Type: application/json'
# ---------------------------------------------------------------------------
bb_api() {
  local method="$1" path="$2"
  shift 2

  : "${BITBUCKET_EMAIL:?BITBUCKET_EMAIL is required for Bitbucket Cloud auth}"
  : "${BITBUCKET_API_TOKEN:?BITBUCKET_API_TOKEN is required for Bitbucket Cloud auth}"

  local attempt=0 max_retries=3 http_code body_file curl_err_file
  body_file=$(mktemp_tracked /tmp/bb-api-body-XXXXXXXX)
  curl_err_file=$(mktemp_tracked /tmp/bb-api-err-XXXXXXXX)

  while true; do
    # Capture curl stderr separately so diagnostic output (TLS errors, DNS
    # failures, connection timeouts) is preserved for the error log rather
    # than discarded. exit-code → 000 sentinel on curl failure.
    http_code=$(curl -sS \
      -u "${BITBUCKET_EMAIL}:${BITBUCKET_API_TOKEN}" \
      -o "$body_file" \
      -w '%{http_code}' \
      -X "$method" \
      "$@" \
      "${BB_API}${path}" 2>"$curl_err_file" || echo "000")

    if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
      cat "$body_file"
      return 0
    fi

    # Retry on transient: 408, 429, 500, 502, 503, 504, and curl failure (000).
    if [[ "$attempt" -lt "$max_retries" ]] && \
       [[ "$http_code" =~ ^(408|429|500|502|503|504|000)$ ]]; then
      attempt=$((attempt + 1))
      local backoff=$(( 2 * (1 << (attempt - 1)) ))
      # Zero-pad jitter to 3 digits so "sleep 2.007" means 7ms, not 700ms.
      local jitter; jitter=$(printf '%03d' $(( RANDOM % 1000 )))
      echo "WARNING: bb_api ${method} ${path} -> ${http_code} (attempt ${attempt}/${max_retries}), retrying in ${backoff}.${jitter}s..." >&2
      sleep "${backoff}.${jitter}"
      continue
    fi

    # Permanent failure or retries exhausted — emit full diagnostics.
    echo "ERROR: bb_api ${method} ${path} -> ${http_code}" >&2
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
  PR_NUMBER="${2:?--get-last-sha requires PR number as second argument}"
  resolve_repo_id

  # Follow Bitbucket's pagination cursor until the marker comment is found or
  # all pages are exhausted. pagelen=100 with the q= filter minimises pages.
  next_url="/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments?pagelen=100&q=content.raw%20~%20%22ai-pr-review-summary%22&sort=-updated_on"
  comment_body=""
  while [[ -n "$next_url" ]]; do
    comments_json=$(bb_api GET "$next_url") || {
      echo "WARNING: get_last_reviewed_sha: Bitbucket API error (treating as first run)." >&2
      exit 0
    }

    # Bitbucket may not honour the `q` filter for rich-text fields; also
    # check the body client-side. Take the first match (sort=-updated_on means
    # most-recently-updated first, so the summary comment appears on page 1).
    comment_body=$(echo "$comments_json" | jq -r --arg marker "$MARKER_PREFIX" \
      '.values // [] | map(select(.content.raw // "" | contains($marker))) | first.content.raw // empty') || {
      echo "WARNING: get_last_reviewed_sha: could not parse comments response (treating as first run)." >&2
      exit 0
    }

    [[ -n "$comment_body" ]] && break

    # Advance to the next page, if any. Strip the base URL prefix so bb_api
    # can prepend BB_API again via the path argument.
    next_url=$(echo "$comments_json" | jq -r '.next // empty' | sed "s|${BB_API}||")
  done

  if [[ -n "$comment_body" ]]; then
    echo "$comment_body" | grep -oE 'sha=[0-9a-f]+' | sed 's/sha=//' | head -1 || true
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Reject --standalone: Bitbucket Cloud has no Issues product.
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--standalone" ]]; then
  echo "ERROR: Standalone review mode is not supported on Bitbucket Cloud (no Issues product)." >&2
  echo "Use REVIEW_TARGET=pr, or VCS_PROVIDER=github for standalone reviews." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Main mode: positional args.
# ---------------------------------------------------------------------------
PR_NUMBER="${1:?Missing PR number}"
SUMMARY_FILE="${2:?Missing summary file}"
# FINDINGS_FILE and DIFF_FILE are accepted for CLI-signature compatibility
# with post-review.sh but unused in v0.2.0 (no inline comments → no diff
# parsing; the FINDINGS_JSON_FILE payload carries everything we render).
# shellcheck disable=SC2034
FINDINGS_FILE="${3:?Missing findings file}"
FINDINGS_JSON_FILE="${4:?Missing findings JSON file}"
# shellcheck disable=SC2034
DIFF_FILE="${5:?Missing diff file}"
HEAD_SHA="${6:?Missing head SHA}"
TOKEN_TABLE_FILE="${7:-}"

# Validate HEAD_SHA is a hex git SHA before it is interpolated into sed
# expressions or embedded in the SHA watermark marker.
if [[ ! "$HEAD_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "ERROR: HEAD_SHA must be a hex git SHA (got '${HEAD_SHA}')." >&2
  exit 1
fi

resolve_repo_id

# ---------------------------------------------------------------------------
# Bitbucket Cloud's content.raw field is capped at 32,768 chars. Truncate at
# 32,000 to leave headroom for JSON encoding and the truncation notice.
# keep in sync with post-review.sh:302 (different cap; GitHub uses 64,000)
# ---------------------------------------------------------------------------
MAX_BODY_SIZE=32000
truncate_body() {
  local body="$1" byte_len
  byte_len=$(printf '%s' "$body" | wc -c)
  if [[ "$byte_len" -gt "$MAX_BODY_SIZE" ]]; then
    local truncated
    # iconv strips incomplete multi-byte codepoints at the cut boundary.
    # //IGNORE exits non-zero when it drops chars, so we can't use || for
    # fallback detection. Instead: run iconv and fall back to raw head -c
    # only if iconv is absent (command -v check) or produces no output.
    local raw_cut
    raw_cut=$(printf '%s' "$body" | head -c "$MAX_BODY_SIZE")
    if command -v iconv > /dev/null 2>&1; then
      truncated=$(printf '%s' "$raw_cut" | iconv -f UTF-8 -t UTF-8//IGNORE 2>/dev/null)
      [[ -z "$truncated" ]] && truncated="$raw_cut"
    else
      truncated="$raw_cut"
    fi
    printf '%s\n\n---\n*Review output truncated — body exceeded Bitbucket Cloud comment limit (32,768 chars). Run a full review locally to see complete output.*\n' \
      "$truncated"
  else
    printf '%s' "$body"
  fi
}

# keep in sync with post-review.sh:672
severity_icon() {
  case "${1,,}" in
    critical) echo "❌" ;;
    high)     echo "🚨" ;;
    medium)   echo "🔶" ;;
    low)      echo "💬" ;;
    *)        echo "⚪" ;;
  esac
}

# keep in sync with post-review.sh:685
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
# Render all findings as markdown bullets (no inline splitting for v0.2.0).
# Writes one bullet per finding to stdout.
# ---------------------------------------------------------------------------
render_findings_markdown() {
  local findings_json="$1"
  local findings_ndjson
  if ! findings_ndjson=$(echo "$findings_json" | jq -c '.[]' 2>/dev/null); then
    echo "WARNING: render_findings_markdown: could not iterate findings JSON; findings section will be empty." >&2
    return 0
  fi

  [[ -z "$findings_ndjson" ]] && return 0

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

    local loc=""
    if [[ -n "$file" && -n "$line" ]]; then
      loc=" — \`${file}:${line}\`"
    elif [[ -n "$file" ]]; then
      loc=" — \`${file}\`"
    fi

    if [[ -n "$remediation" ]]; then
      printf -- '- %s **[%s]** %s %s%s\n  - **Remediation:** %s\n' \
        "$(severity_icon "$severity")" "$severity" "$source_tag" "$finding" "$loc" "$remediation"
    else
      printf -- '- %s **[%s]** %s %s%s\n' \
        "$(severity_icon "$severity")" "$severity" "$source_tag" "$finding" "$loc"
    fi
  done <<< "$findings_ndjson"
}

# ---------------------------------------------------------------------------
# Classify overall risk from the findings JSON. Prints "<risk>|<event>" where
# risk ∈ {None, Low, Medium, High, Critical, Unknown} and event ∈ {APPROVE,
# REQUEST_CHANGES, COMMENT}. event is informational only on Bitbucket (we
# don't call approve/request-changes endpoints in v0.2.0) but we reuse it
# to pick the body heading.
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
# Build the complete comment body: marker + summary + findings + tokens.
# ---------------------------------------------------------------------------
build_comment_body() {
  local summary findings_json

  if [[ ! -f "$SUMMARY_FILE" ]]; then
    echo "ERROR: Summary file not found: ${SUMMARY_FILE}. This indicates an upstream agent failure." >&2
    return 1
  fi
  summary=$(cat "$SUMMARY_FILE")

  # Strip mermaid fenced blocks — Bitbucket PR comments do not render mermaid.
  summary=$(printf '%s' "$summary" | awk '
    /^```mermaid/ { skip=1; next }
    skip && /^```/ { skip=0; next }
    !skip { print }
  ')

  findings_json="[]"
  if [[ -f "$FINDINGS_JSON_FILE" ]]; then
    findings_json=$(cat "$FINDINGS_JSON_FILE")
    if ! echo "$findings_json" | jq -e 'type == "array"' > /dev/null 2>&1; then
      echo "ERROR: Findings JSON file is not a valid JSON array. Review results may be incomplete." >&2
      echo "       Re-run the pipeline to retry. File: ${FINDINGS_JSON_FILE}" >&2
      return 1
    fi
  fi

  local risk_event risk event finding_total
  risk_event=$(classify_risk "$findings_json")
  risk="${risk_event%|*}"
  event="${risk_event#*|}"
  finding_total=$(echo "$findings_json" | jq 'length')

  local findings_md=""
  if [[ "$finding_total" -gt 0 ]]; then
    findings_md=$(render_findings_markdown "$findings_json")
  fi

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
    summary_block="$(severity_icon "$risk") **Overall Risk:** ${risk} | **Findings:** ${finding_total}

No Critical or High findings. The changes look good — Medium/Low findings are informational only."
    findings_block="### Findings (informational)
${findings_md}"
  else
    heading="## AI Review Findings"
    summary_block="$(severity_icon "$risk") **Overall Risk:** ${risk} | **Findings:** ${finding_total}"
    findings_block="### Findings
${findings_md}"
  fi

  # Embed the SHA marker at the top so get_last_reviewed_sha can find it.
  local sha_marker="${MARKER_PREFIX} sha=${HEAD_SHA} -->"

  # Summary text from the pr-summarizer agent (may be empty).
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
${token_block}
---
*AI Review — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"

  truncate_body "$body"
}

# ---------------------------------------------------------------------------
# Find an existing summary comment by marker. Returns the comment id on
# stdout, or empty if none exists.
# ---------------------------------------------------------------------------
find_existing_summary_id() {
  # Same server-side filter and sort as --get-last-sha. sort=-updated_on puts
  # the summary comment (always recently touched) on the first page so we
  # rarely need to paginate. The jq contains() check below is a safety net
  # since Bitbucket may not always honour `q` on rich-text fields.
  local next_url="/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments?pagelen=100&q=content.raw%20~%20%22ai-pr-review-summary%22&sort=-updated_on"
  while [[ -n "$next_url" ]]; do
    local comments_json
    comments_json=$(bb_api GET "$next_url") || return 1

    local found_id
    found_id=$(echo "$comments_json" | jq -r --arg marker "$MARKER_PREFIX" \
      '.values // [] | map(select(.content.raw // "" | contains($marker))) | first.id // empty') || {
      echo "WARNING: find_existing_summary_id: could not parse comments response." >&2
      return 1
    }

    [[ -n "$found_id" ]] && { echo "$found_id"; return 0; }

    next_url=$(echo "$comments_json" | jq -r '.next // empty' | sed "s|${BB_API}||")
  done
  # No matching comment found across all pages.
  echo ""
}

# ---------------------------------------------------------------------------
# Upsert the summary comment. POST on first run, PUT on subsequent runs.
# ---------------------------------------------------------------------------
post_summary_with_findings() {
  local body
  body=$(build_comment_body) || return 1

  local payload_file
  payload_file=$(mktemp_tracked /tmp/bb-comment-XXXXXXXX.json)
  if ! jq -n --arg raw "$body" '{content: {raw: $raw}}' > "$payload_file"; then
    echo "ERROR: Failed to build comment payload (jq failed)." >&2
    return 1
  fi

  local existing_id
  # Propagate API errors from find_existing_summary_id rather than treating
  # them as "no existing comment" — the latter causes duplicate POSTs on
  # transient GET failures.
  if ! existing_id=$(find_existing_summary_id); then
    echo "ERROR: Could not query existing comments; cannot safely upsert." >&2
    return 1
  fi

  local result new_id
  if [[ -n "$existing_id" ]]; then
    echo "Updating existing summary comment #${existing_id}..." >&2
    if result=$(bb_api PUT \
      "/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments/${existing_id}" \
      -H 'Content-Type: application/json' \
      --data-binary "@${payload_file}"); then
      echo "Summary comment updated on PR #${PR_NUMBER}." >&2
      _cleanup_duplicate_summary_comments "$existing_id"
      return 0
    fi
    echo "ERROR: Failed to update summary comment #${existing_id}." >&2
    return 1
  fi

  echo "Posting new summary comment..." >&2
  if result=$(bb_api POST \
    "/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments" \
    -H 'Content-Type: application/json' \
    --data-binary "@${payload_file}"); then
    new_id=$(echo "$result" | jq -r '.id // empty')
    echo "Summary comment posted to PR #${PR_NUMBER} (id=${new_id:-unknown})." >&2
    _cleanup_duplicate_summary_comments "${new_id:-}"
    return 0
  fi
  echo "ERROR: Failed to post summary comment to PR #${PR_NUMBER}. Review results were not delivered." >&2
  return 1
}

# Delete all summary-marker comments except the one we just kept. Non-fatal:
# duplicates are cosmetic; we never block the review on cleanup failures.
_cleanup_duplicate_summary_comments() {
  local kept_id="$1"
  [[ -z "$kept_id" ]] && return 0

  local next_url="/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments?pagelen=100&q=content.raw%20~%20%22ai-pr-review-summary%22"
  while [[ -n "$next_url" ]]; do
    local page_json
    page_json=$(bb_api GET "$next_url" 2>/dev/null) || break

    while IFS= read -r dup_id; do
      [[ -z "$dup_id" || "$dup_id" == "$kept_id" ]] && continue
      echo "Removing duplicate summary comment #${dup_id}..." >&2
      bb_api DELETE \
        "/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments/${dup_id}" \
        > /dev/null 2>&1 \
        || echo "WARNING: Could not delete duplicate summary comment #${dup_id}." >&2
    done < <(echo "$page_json" | jq -r --arg marker "$MARKER_PREFIX" \
      '.values // [] | map(select(.content.raw // "" | contains($marker))) | .[].id' 2>/dev/null)

    next_url=$(echo "$page_json" | jq -r '.next // empty' | sed "s|${BB_API}||")
  done
}

# ---------------------------------------------------------------------------
# Advance the SHA watermark in the existing summary comment.
# keep in sync with post-review.sh:1021
# ---------------------------------------------------------------------------
update_sha_marker() {
  local existing_id existing_body
  existing_id=$(find_existing_summary_id || true)
  if [[ -z "$existing_id" ]]; then
    echo "No existing summary comment found; SHA marker not updated." >&2
    return 0
  fi

  existing_body=$(bb_api GET \
    "/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments/${existing_id}" \
    | jq -r '.content.raw // empty') || {
    echo "WARNING: Could not fetch summary comment body for SHA update." >&2
    return 0
  }

  # Replace the sha= value in the marker. Use sed (not bash parameter
  # expansion) because the pattern is a regex, not a literal string.
  local updated_body
  # shellcheck disable=SC2001  # regex replace; parameter expansion can't match [0-9a-f]*
  updated_body=$(echo "$existing_body" | sed "s|${MARKER_PREFIX} sha=[0-9a-f]* -->|${MARKER_PREFIX} sha=${HEAD_SHA} -->|")

  if [[ "$updated_body" == "$existing_body" ]]; then
    echo "SHA marker already at ${HEAD_SHA}; no update needed." >&2
    return 0
  fi

  local payload_file
  payload_file=$(mktemp_tracked /tmp/bb-sha-XXXXXXXX.json)
  jq -n --arg raw "$updated_body" '{content: {raw: $raw}}' > "$payload_file"

  if bb_api PUT \
    "/repositories/${WORKSPACE}/${REPO_SLUG}/pullrequests/${PR_NUMBER}/comments/${existing_id}" \
    -H 'Content-Type: application/json' \
    --data-binary "@${payload_file}" > /dev/null; then
    echo "SHA marker advanced to ${HEAD_SHA}." >&2
  else
    echo "WARNING: Failed to update SHA marker in summary comment." >&2
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "--- Posting review to Bitbucket Cloud PR #${PR_NUMBER} ---" >&2

# Findings are rendered inside the summary body in v0.2.0 (no inline comments).
# build_comment_body embeds HEAD_SHA directly, so the SHA watermark is always
# current after a successful post — no separate update_sha_marker call needed.
if ! post_summary_with_findings; then
  echo "ERROR: Review results were not delivered to PR #${PR_NUMBER}." >&2
  exit 1
fi
