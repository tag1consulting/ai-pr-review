#!/usr/bin/env bash
#
# post-review.sh — Post AI review results to a GitHub PR.
#
# Posts Block A (summary) as a PR comment and Block B (findings) as a pull request review
# with inline comments.
#
# Usage:
#   ./post-review.sh <pr_number> <summary_file> <findings_file> <findings_json_file> <diff_file> <head_sha>
#
# Environment:
#   GH_TOKEN     — GitHub token for API access
#   GITHUB_REPOSITORY — owner/repo (set automatically in GitHub Actions)

set -euo pipefail

# ---------------------------------------------------------------------------
# --get-last-sha mode: must be checked before positional param validation
# because it is invoked with only one argument.
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--get-last-sha" ]]; then
  : "${GH_TOKEN:?GH_TOKEN is required}"
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
  OWNER="${GITHUB_REPOSITORY%%/*}"
  REPO="${GITHUB_REPOSITORY##*/}"
  PR_NUMBER="${2:?--get-last-sha requires PR number as second argument}"
  MARKER_PREFIX="<!-- ai-pr-review-summary"

  get_last_reviewed_sha() {
    local comment_body gh_err
    gh_err=$(mktemp)
    comment_body=$(gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
      --paginate \
      --jq "[.[] | select(.body | contains(\"${MARKER_PREFIX}\")) | .body] | last // empty" \
      2>"$gh_err") || {
      echo "WARNING: get_last_reviewed_sha: GitHub API error (treating as first run): $(cat "$gh_err")" >&2
      rm -f "$gh_err"
      return 0
    }
    rm -f "$gh_err"
    if [[ -n "$comment_body" ]]; then
      echo "$comment_body" | grep -oE 'sha=[0-9a-f]+' | sed 's/sha=//' | head -1 || true
    fi
  }

  get_last_reviewed_sha
  exit 0
fi

# ---------------------------------------------------------------------------
# gh_api_retry — retry gh api calls on transient failures (502, 503, 429).
# Usage: gh_api_retry [gh api args...]
# Retries up to 3 times with exponential backoff (2s, 4s, 8s) + jitter.
# ---------------------------------------------------------------------------
gh_api_retry() {
  local attempt=0 max_retries=3 result exit_code

  # Capture stdin so retries can re-read piped request bodies (e.g.,
  # echo "$json" | gh_api_retry api ... --input -). Without this, stdin
  # is consumed on the first gh invocation and retries get empty input.
  local stdin_file
  stdin_file=$(mktemp /tmp/gh-retry-stdin-XXXXXXXX)
  cat > "$stdin_file"

  # Rewrite --input - to --input <file> so each retry re-reads the body
  local -a gh_args=()
  local prev=""
  for arg in "$@"; do
    if [[ "$prev" == "--input" && "$arg" == "-" ]]; then
      gh_args+=("$stdin_file")
    else
      gh_args+=("$arg")
    fi
    prev="$arg"
  done

  while true; do
    exit_code=0
    result=$(gh "${gh_args[@]}" 2>&1) || exit_code=$?
    if [[ "$exit_code" -eq 0 ]]; then
      rm -f "$stdin_file"
      printf '%s' "$result"
      # Record VCS tape when AI_PR_REVIEW_RECORD_DIR is set.
      if [[ -n "${AI_PR_REVIEW_RECORD_DIR:-}" ]]; then
        local _tape_url _tape_method _tape_arg _tape_saw_method=false
        _tape_url="" _tape_method="GET"
        for _tape_arg in "${gh_args[@]}"; do
          if [[ "$_tape_saw_method" == "true" ]]; then
            _tape_method="$_tape_arg"
            _tape_saw_method=false
          elif [[ "$_tape_arg" == "-X" || "$_tape_arg" == "--method" ]]; then
            _tape_saw_method=true
          elif [[ "$_tape_arg" =~ ^repos/ || "$_tape_arg" =~ ^orgs/ || "$_tape_arg" =~ ^graphql ]]; then
            _tape_url="https://api.github.com/$_tape_arg"
          fi
        done
        local _resp_file; _resp_file=$(mktemp /tmp/gh-tape-resp-XXXXXXXX)
        printf '%s' "$result" > "$_resp_file"
        if [[ -z "${_tape_url:-}" ]]; then
          echo "WARNING: could not extract tape URL from gh api args; skipping tape for this call" >&2
        else
          record_tape "github" "$_tape_method" "$_tape_url" "$stdin_file" "$_resp_file"
        fi
        rm -f "$_resp_file"
      fi
      return 0
    fi
    # Check if the error looks transient
    if [[ "$attempt" -lt "$max_retries" ]] && echo "$result" | grep -qE '(502|503|429|ETIMEDOUT|Server Error|rate limit)'; then
      attempt=$((attempt + 1))
      local backoff=$(( 2 * (1 << (attempt - 1)) ))
      local jitter=$(( RANDOM % 1000 ))  # milliseconds, formatted as fractional seconds for sleep
      echo "WARNING: gh api call failed (attempt ${attempt}/${max_retries}), retrying in ${backoff}.${jitter}s..." >&2
      sleep "${backoff}.${jitter}"
      continue
    fi
    # Not transient or retries exhausted
    rm -f "$stdin_file"
    printf '%s' "$result"
    return "$exit_code"
  done
}

# ---------------------------------------------------------------------------
# --standalone mode: post findings as a GitHub issue instead of a PR review.
# Usage: post-review.sh --standalone <summary_file> <findings_file>
#        <findings_json_file> <diff_file> <head_sha> [token_table_file]
# Env: GH_TOKEN, GITHUB_REPOSITORY, BASE_REF
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--standalone" ]]; then
  shift
  _SUMMARY_FILE="${1:?Missing summary file}"
  _FINDINGS_FILE="${2:?Missing findings file}"
  _FINDINGS_JSON_FILE="${3:?Missing findings JSON file}"
  _DIFF_FILE="${4:?Missing diff file}"
  _HEAD_SHA="${5:?Missing head SHA}"
  _TOKEN_TABLE_FILE="${6:-}"

  : "${GH_TOKEN:?GH_TOKEN is required}"
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
  : "${BASE_REF:?BASE_REF is required for standalone mode}"

  _OWNER="${GITHUB_REPOSITORY%%/*}"
  _REPO="${GITHUB_REPOSITORY##*/}"

  _severity_icon() {
    case "${1,,}" in
      critical) echo "❌" ;; high) echo "🚨" ;;
      medium)   echo "🔶" ;; low)  echo "💬" ;;
      *)        echo "⚪" ;;
    esac
  }

  _truncate_body() {
    local body="$1" limit=64000 byte_len
    byte_len=$(printf '%s' "$body" | wc -c)
    if [[ "$byte_len" -gt "$limit" ]]; then
      local truncated
      truncated=$(printf '%s' "$body" | head -c "$limit" | iconv -f UTF-8 -t UTF-8//IGNORE 2>/dev/null)
      printf '%s\n\n---\n*Review output truncated — body exceeded GitHub API limit.*\n' \
        "$truncated"
    else
      printf '%s' "$body"
    fi
  }

  post_standalone_issue() {
    local summary findings_json finding_total overall_risk
    summary=$(cat "$_SUMMARY_FILE")
    findings_json="[]"
    if [[ -f "$_FINDINGS_JSON_FILE" ]]; then
      if jq -e 'type == "array"' "$_FINDINGS_JSON_FILE" > /dev/null 2>&1; then
        findings_json=$(cat "$_FINDINGS_JSON_FILE")
      fi
    fi
    finding_total=$(echo "$findings_json" | jq 'length')

    if [[ "$finding_total" -eq 0 ]]; then
      overall_risk="None"
    elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "critical")' > /dev/null 2>&1; then
      overall_risk="Critical"
    elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "high")' > /dev/null 2>&1; then
      overall_risk="High"
    elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "medium")' > /dev/null 2>&1; then
      overall_risk="Medium"
    else
      overall_risk="Low"
    fi

    local icon
    icon=$(_severity_icon "$overall_risk")

    # Build issue body
    local issue_body
    issue_body="<!-- ai-standalone-review sha=${_HEAD_SHA} -->
## AI Code Review

${icon} **Overall Risk:** ${overall_risk} | **Findings:** ${finding_total} | **Commit:** \`${_HEAD_SHA:0:12}\` | **Base:** \`${BASE_REF}\`

### Summary

${summary}
"

    if [[ "$finding_total" -gt 0 ]]; then
      issue_body="${issue_body}
### Findings

$(  _enable_lc="${AI_ENABLE_SUGGESTIONS:-true}"
    _enable_lc="${_enable_lc,,}"
    if [[ "$_enable_lc" == "true" ]]; then _jq_enable=true; else _jq_enable=false; fi
    echo "$findings_json" | jq -r --argjson enable_suggestions "$_jq_enable" '
  sort_by(
    if (.severity | ascii_downcase) == "critical" then 0
    elif (.severity | ascii_downcase) == "high" then 1
    elif (.severity | ascii_downcase) == "medium" then 2
    else 3 end
  ) | .[] |
  (
    if (.sources | type) == "array" and (.sources | length) > 1 then
      "[\(.sources[0])] *(also flagged by: \(.sources[1:] | join(", ")))*"
    else
      "[" + (.sources[0] // .source // "unknown") + "]"
    end
  ) as $stag |
  "- **[\(.severity)]** \($stag) `\(.file // "unknown"):\(.line // "?")` — \(.finding)\n  **Remediation:** \(.remediation // "N/A")\n" +
  if $enable_suggestions and ((.suggested_code // "") | length) > 0 and (.suggested_code | contains("```") | not) then
    "  <details>\n  <summary>Suggested fix</summary>\n\n  ```\n" +
    (.suggested_code | split("\n") | map("  " + .) | join("\n")) +
    "\n  ```\n\n  </details>\n"
  else "" end
')"
    else
      issue_body="${issue_body}
No findings above the confidence threshold. The code looks good."
    fi

    if [[ -n "$_TOKEN_TABLE_FILE" && -s "$_TOKEN_TABLE_FILE" ]]; then
      issue_body="${issue_body}

$(cat "$_TOKEN_TABLE_FILE")"
    fi

    local failed_agents_env="${AI_REVIEW_FAILED_AGENTS:-}"
    if [[ -n "$failed_agents_env" ]]; then
      issue_body="${issue_body}

> **Warning:** The following agents failed and the review may be incomplete: ${failed_agents_env//:/, }"
    fi

    if [[ "$finding_total" -gt 0 ]]; then
      local _agent_prompt
      _agent_prompt=$(echo "$findings_json" | jq -r '
        group_by(.file) | map(
          "In `\(.[0].file)`:" as $header |
          [$header] + [
            .[] |
            "- Around line \(.line // "?"): \(.finding)" +
              if (.remediation // "") != "" then ". " + .remediation else "" end
          ] | join("\n")
        ) | join("\n\n")
      ' 2>/dev/null)
      if [[ -n "$_agent_prompt" ]]; then
        issue_body="${issue_body}

<details>
<summary>🤖 Prompt for AI agents</summary>

\`\`\`
Verify each finding against the current code and only fix it if needed.

${_agent_prompt}
\`\`\`

</details>"
      fi
    fi

    issue_body="${issue_body}

---
*AI Code Review — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"

    issue_body=$(_truncate_body "$issue_body")

    local issue_title="${icon} AI Review: ${overall_risk} risk — ${_HEAD_SHA:0:7} on ${BASE_REF}"

    echo "Creating standalone review issue..." >&2

    # Attempt with labels; fall back without if labels don't exist on the repo
    local label_args=(--field "labels[]=ai-review")
    if [[ "$overall_risk" == "Critical" || "$overall_risk" == "High" ]]; then
      label_args+=(--field "labels[]=ai-review-action-needed")
    fi

    local issue_url issue_err
    issue_err=$(mktemp)
    if ! issue_url=$(gh_api_retry api "repos/${_OWNER}/${_REPO}/issues" \
      --method POST \
      --field title="$issue_title" \
      --field body="$issue_body" \
      "${label_args[@]}" \
      --jq '.html_url' 2>"$issue_err"); then
      local first_err
      first_err=$(cat "$issue_err")
      echo "WARNING: Issue creation with labels failed (${first_err}); retrying without labels..." >&2
      if ! issue_url=$(gh_api_retry api "repos/${_OWNER}/${_REPO}/issues" \
        --method POST \
        --field title="$issue_title" \
        --field body="$issue_body" \
        --jq '.html_url' 2>"$issue_err"); then
        echo "ERROR: Failed to create standalone review issue: $(cat "$issue_err")" >&2
        rm -f "$issue_err"
        exit 1
      fi
    fi
    rm -f "$issue_err"

    echo "Standalone review issue created: ${issue_url}" >&2
  }

  post_standalone_issue
  exit 0
fi

PR_NUMBER="${1:?Usage: post-review.sh <pr_number> <summary_file> <findings_file> <findings_json_file> <diff_file> <head_sha> [token_table_file]}"
SUMMARY_FILE="${2:?Missing summary file}"
FINDINGS_FILE="${3:?Missing findings file}"
FINDINGS_JSON_FILE="${4:?Missing findings JSON file}"
DIFF_FILE="${5:?Missing diff file}"
HEAD_SHA="${6:?Missing head SHA}"
TOKEN_TABLE_FILE="${7:-}"

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

OWNER="${GITHUB_REPOSITORY%%/*}"
REPO="${GITHUB_REPOSITORY##*/}"
MARKER_PREFIX="<!-- ai-pr-review-summary"
# MARKER_PREFIX is embedded in comment bodies to identify our summary comments.
# The full marker includes an optional sha= field: <!-- ai-pr-review-summary sha=<sha> -->

# Temp files — cleaned up on exit. Used by mktemp_tracked/cleanup in vcs/common.sh.
# shellcheck disable=SC2034
TMPFILES=()

# Shared helpers (severity_icon, mktemp_tracked, cleanup, format_source_tag,
# format_body_finding, build_agent_prompt, classify_risk, parse_valid_lines,
# parse_diff_new_lines). MAX_BODY_SIZE / truncate_body stay below because
# their truncation message is provider-specific.
# shellcheck source=vcs/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vcs/common.sh"
# shellcheck source=lib/finding-ids.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/finding-ids.sh"

trap cleanup EXIT

# GitHub API rejects comment/review bodies exceeding 65,536 bytes (the limit
# applies to the JSON-encoded UTF-8 request body, not character count).
# Truncate at 64,000 bytes to leave room for the truncation notice itself.
MAX_BODY_SIZE=64000
truncate_body() {
  local body="$1" byte_len
  byte_len=$(printf '%s' "$body" | wc -c)
  if [[ "$byte_len" -gt "$MAX_BODY_SIZE" ]]; then
    local truncated
    # head -c cuts at a byte boundary which may land mid-UTF-8-sequence;
    # iconv -t UTF-8//IGNORE drops any trailing partial codepoint so the
    # output is always valid UTF-8.
    truncated=$(printf '%s' "$body" | head -c "$MAX_BODY_SIZE" | iconv -f UTF-8 -t UTF-8//IGNORE 2>/dev/null)
    printf '%s\n\n---\n*Review output truncated — body exceeded GitHub API limit (65,536 bytes). Run a full review locally to see complete output.*\n' \
      "$truncated"
  else
    printf '%s' "$body"
  fi
}

# ---------------------------------------------------------------------------
# Find the last-reviewed SHA from the existing summary comment.
# Returns the SHA via stdout, or empty string if no prior review.
# ---------------------------------------------------------------------------
get_last_reviewed_sha() {
  local comment_body gh_err
  gh_err=$(mktemp)
  comment_body=$(gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
    --paginate --jq "[.[] | select(.body | contains(\"${MARKER_PREFIX}\")) | .body] | last // empty" \
    2>"$gh_err") || {
    echo "WARNING: get_last_reviewed_sha: GitHub API error (treating as first run): $(cat "$gh_err")" >&2
    rm -f "$gh_err"
    return 0
  }
  rm -f "$gh_err"

  if [[ -n "$comment_body" ]]; then
    # Extract sha= value from marker: <!-- ai-pr-review-summary sha=abc1234 -->
    echo "$comment_body" | grep -oE 'sha=[0-9a-f]+' | sed 's/sha=//' | head -1 || true
  fi
}

# ---------------------------------------------------------------------------
# Auto-resolve unresolved review threads posted by github-actions[bot]
# ---------------------------------------------------------------------------
resolve_stale_threads() {
  echo "Resolving stale review threads..." >&2

  # Fetch all review threads, paginating in batches of 100
  local threads_json="[]"
  local cursor=""
  local has_next=true

  while [[ "$has_next" == "true" ]]; do
    local cursor_arg=()
    if [[ -n "$cursor" ]]; then
      cursor_arg=(-f after="$cursor")
    fi

    local page_result
    page_result=$(gh api graphql -f query='
      query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
        repository(owner: $owner, name: $repo) {
          pullRequest(number: $pr) {
            reviewThreads(first: 100, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes {
                id
                isResolved
                comments(first: 1) {
                  nodes {
                    author {
                      login
                    }
                  }
                }
              }
            }
          }
        }
      }' \
      -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" \
      "${cursor_arg[@]}" 2>/dev/null) || {
      echo "WARNING: Could not fetch review threads for resolution." >&2
      return 0
    }

    local page_nodes
    page_nodes=$(echo "$page_result" | jq '.data.repository.pullRequest.reviewThreads.nodes' 2>/dev/null) || {
      echo "WARNING: jq parse failed on reviewThreads page; proceeding with partial data." >&2
      break
    }
    threads_json=$(echo "$threads_json" "$page_nodes" | jq -s '.[0] + .[1]')

    has_next=$(echo "$page_result" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' 2>/dev/null)
    cursor=$(echo "$page_result" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' 2>/dev/null)
  done

  # Filter to unresolved threads posted by github-actions[bot]
  local thread_ids
  thread_ids=$(echo "$threads_json" | jq -r '
    .[] | select(
      .isResolved == false and
      (.comments.nodes[0].author.login // "") == "github-actions[bot]"
    ) | .id
  ' 2>/dev/null) || true

  if [[ -z "$thread_ids" ]]; then
    echo "No stale threads to resolve." >&2
    return 0
  fi

  local resolved=0 failed=0
  while IFS= read -r thread_id; do
    [[ -z "$thread_id" ]] && continue
    local resolve_result
    resolve_result=$(gh api graphql -f query='
      mutation($threadId: ID!) {
        resolveReviewThread(input: {threadId: $threadId}) {
          thread { id isResolved }
        }
      }' \
      -f threadId="$thread_id" 2>&1) || {
      echo "WARNING: Could not resolve thread ${thread_id}: ${resolve_result}" >&2
      failed=$(( failed + 1 ))
      continue
    }
    resolved=$(( resolved + 1 ))
  done <<< "$thread_ids"

  if [[ "$failed" -gt 0 ]]; then
    echo "Resolved ${resolved} stale review thread(s); ${failed} failed to resolve." >&2
  else
    echo "Resolved ${resolved} stale review thread(s)." >&2
  fi
}

# ---------------------------------------------------------------------------
# Dismiss stale CHANGES_REQUESTED reviews from github-actions[bot] whose
# threads are all resolved. Prevents old blocking reviews from accumulating.
# ---------------------------------------------------------------------------
dismiss_stale_reviews() {
  echo "Checking for stale CHANGES_REQUESTED reviews to dismiss..." >&2

  # Find all CHANGES_REQUESTED reviews submitted by github-actions[bot]
  local reviews_json
  reviews_json=$(gh api "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews" \
    --paginate --jq '[.[] | select(.state == "CHANGES_REQUESTED" and .user.login == "github-actions[bot]") | {id: .id}]' \
    2>/dev/null) || {
    echo "WARNING: Could not fetch reviews for dismissal check." >&2
    return 0
  }

  local review_ids
  if ! review_ids=$(echo "$reviews_json" | jq -r '.[].id' 2>/dev/null); then
    echo "WARNING: jq parse failed on reviews response; skipping dismiss." >&2
    return 0
  fi

  if [[ -z "$review_ids" ]]; then
    echo "No stale CHANGES_REQUESTED reviews to dismiss." >&2
    return 0
  fi

  # Protect the newest bot review (highest ID) — it represents the current run
  # and must never be dismissed, even if all its threads are resolved.
  # Only reviews older than the newest are eligible for dismissal.
  local newest_review_id
  newest_review_id=$(echo "$review_ids" | sort -n | tail -1)
  if [[ -z "$newest_review_id" ]]; then
    echo "WARNING: Could not determine newest review ID; skipping dismiss." >&2
    return 0
  fi
  local review_count
  review_count=$(echo "$review_ids" | wc -l | tr -d ' ')
  if [[ "$review_count" -lt 2 ]]; then
    echo "Only one bot CHANGES_REQUESTED review exists — nothing to dismiss." >&2
    return 0
  fi

  # Fetch all review threads (paginated) to count unresolved threads per review
  local all_threads="[]"
  local cursor=""
  local has_next=true

  while [[ "$has_next" == "true" ]]; do
    local cursor_arg=()
    if [[ -n "$cursor" ]]; then
      cursor_arg=(-f after="$cursor")
    fi

    local page_result
    page_result=$(gh api graphql -f query='
      query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
        repository(owner: $owner, name: $repo) {
          pullRequest(number: $pr) {
            reviewThreads(first: 100, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes {
                isResolved
                comments(first: 1) {
                  nodes { pullRequestReview { databaseId } }
                }
              }
            }
          }
        }
      }' \
      -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER" \
      "${cursor_arg[@]}" 2>/dev/null) || {
      echo "WARNING: Could not fetch review threads for dismissal check." >&2
      return 0
    }

    local page_nodes
    page_nodes=$(echo "$page_result" | jq '.data.repository.pullRequest.reviewThreads.nodes' 2>/dev/null) || {
      echo "WARNING: jq parse failed on reviewThreads page; proceeding with partial data." >&2
      break
    }
    all_threads=$(echo "$all_threads" "$page_nodes" | jq -s '.[0] + .[1]')

    has_next=$(echo "$page_result" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' 2>/dev/null)
    cursor=$(echo "$page_result" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' 2>/dev/null)
  done

  local dismissed=0
  while IFS= read -r review_id; do
    [[ -z "$review_id" ]] && continue
    if ! [[ "$review_id" =~ ^[0-9]+$ ]]; then
      echo "WARNING: skipping non-numeric review_id: ${review_id}" >&2
      continue
    fi
    # Never dismiss the newest (current) bot review.
    if [[ "$review_id" == "$newest_review_id" ]]; then
      continue
    fi
    # Count unresolved threads belonging to this review from pre-fetched data
    local unresolved_count
    unresolved_count=$(echo "$all_threads" | jq \
      --argjson rid "$review_id" \
      '[.[] | select(.comments.nodes[0].pullRequestReview.databaseId == $rid and .isResolved == false)] | length' \
      2>/dev/null) || unresolved_count=1  # assume not safe to dismiss on error
    # Guard against non-integer output (null, float, error string) from jq
    if ! [[ "${unresolved_count:-}" =~ ^[0-9]+$ ]]; then
      unresolved_count=1
    fi

    if [[ "$unresolved_count" -eq 0 ]]; then
      local dismiss_result
      dismiss_result=$(gh api "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews/${review_id}/dismissals" \
        --method PUT \
        --field message="Superseded by a subsequent review run." \
        2>&1) && {
        echo "Dismissed stale review #${review_id}." >&2
        dismissed=$(( dismissed + 1 ))
      } || echo "WARNING: Could not dismiss review #${review_id}: ${dismiss_result}" >&2
    fi
  done <<< "$review_ids"

  echo "Dismissed ${dismissed} stale review(s)." >&2
}

# ---------------------------------------------------------------------------
# Post Block A: Summary comment (idempotent via marker, embeds reviewed SHA)
# ---------------------------------------------------------------------------
post_summary() {
  local summary
  summary=$(cat "$SUMMARY_FILE")

  if [[ -z "$summary" ]]; then
    echo "No summary to post." >&2
    return 0
  fi

  # Embed the HEAD_SHA in the marker so subsequent runs can find the last-reviewed SHA.
  # Truncate only the summary text — the sha_marker must always be present at the top
  # so get_last_reviewed_sha() can extract it even on very large summaries.
  local sha_marker="${MARKER_PREFIX} sha=${HEAD_SHA} -->"
  local merge_filter_note=""
  if [[ -n "${AI_MERGE_FILTER_FALLBACK_REASON:-}" ]]; then
    merge_filter_note=$'\n'"_Note: merge-commit filtering was skipped (${AI_MERGE_FILTER_FALLBACK_REASON}); diff may include upstream changes._"$'\n'
  fi
  local truncated_summary
  truncated_summary=$(truncate_body "${merge_filter_note}${summary}")
  local body="${sha_marker}
${truncated_summary}

---
*AI Review Summary — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"

  # Find existing summary comment by marker prefix
  local existing_comment_id
  existing_comment_id=$(gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
    --paginate \
    --jq ".[] | select(.body | contains(\"${MARKER_PREFIX}\")) | .id" \
    2>/dev/null | head -1) || true

  local kept_comment_id
  if [[ -n "$existing_comment_id" ]]; then
    echo "Updating existing summary comment #${existing_comment_id}..." >&2
    gh_api_retry api "repos/${OWNER}/${REPO}/issues/comments/${existing_comment_id}" \
      --method PATCH \
      --field body="$body" > /dev/null || {
      echo "ERROR: Failed to update summary comment #${existing_comment_id}." >&2
      return 1
    }
    kept_comment_id="$existing_comment_id"
  else
    echo "Posting new summary comment..." >&2
    local new_comment_id
    new_comment_id=$(gh_api_retry api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
      --method POST \
      --field body="$body" \
      --jq ".id" < /dev/null) || {
      echo "ERROR: Failed to post summary comment." >&2
      return 1
    }
    if [[ -z "$new_comment_id" ]]; then
      echo "ERROR: Posted summary comment but could not capture its ID." >&2
      return 1
    fi
    kept_comment_id="$new_comment_id"
  fi

  _cleanup_duplicate_summary_comments "$kept_comment_id"

  echo "Summary comment posted to PR #${PR_NUMBER}." >&2
}

# Delete all summary marker comments except the one identified by kept_id.
# Cosmetic failures are non-fatal — the next run will clean up any leftovers.
_cleanup_duplicate_summary_comments() {
  local kept_id="$1"
  # Safety: if kept_id is empty, grep -v "^$" would pass all IDs for deletion.
  [[ -z "$kept_id" ]] && return 0
  local duplicate_ids listing
  listing=$(gh_api_retry api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
    --paginate \
    --jq ".[] | select(.body | contains(\"${MARKER_PREFIX}\")) | .id" \
    < /dev/null 2>&1) || {
    echo "WARNING: Could not list summary comments for cleanup: ${listing}" >&2
    return 0
  }
  duplicate_ids=$(printf '%s\n' "$listing" | grep -v "^${kept_id}$") || true
  [[ -z "$duplicate_ids" ]] && return 0
  while IFS= read -r dup_id; do
    [[ -z "$dup_id" ]] && continue
    echo "Deleting duplicate summary comment #${dup_id}..." >&2
    gh_api_retry api "repos/${OWNER}/${REPO}/issues/comments/${dup_id}" \
      --method DELETE > /dev/null < /dev/null \
      || echo "WARNING: Failed to delete duplicate summary comment #${dup_id}." >&2
  done <<< "$duplicate_ids"
}


# ---------------------------------------------------------------------------
# Post Block B: Findings as a pull request review with inline comments
# ---------------------------------------------------------------------------
post_findings() {
  local findings agent_prompt=""
  findings=$(cat "$FINDINGS_FILE")

  if [[ -z "$findings" || "$findings" == "NONE" ]]; then
    echo "No findings to post." >&2
    return 0
  fi

  # Parse the JSON findings for inline comments
  local findings_json="[]"
  if [[ -f "$FINDINGS_JSON_FILE" ]]; then
    findings_json=$(cat "$FINDINGS_JSON_FILE")
    # Validate it's valid JSON array
    if ! echo "$findings_json" | jq -e 'type == "array"' > /dev/null 2>&1; then
      echo "WARNING: Invalid findings JSON, posting as body-only review." >&2
      findings_json="[]"
    fi
  fi

  # Build valid lines lookup from diff
  local valid_lines_file
  valid_lines_file=$(mktemp_tracked /tmp/valid-lines-XXXXXXXX.txt)
  parse_valid_lines "$DIFF_FILE" > "$valid_lines_file"

  # When suggestions are enabled, also build a lookup of all new-file lines
  # (added + context) to validate multi-line suggestion ranges.
  # Case-insensitive comparison so TRUE/True/true all work consistently.
  local diff_lines_file=""
  local _enable_for_lookup="${AI_ENABLE_SUGGESTIONS:-true}"
  _enable_for_lookup="${_enable_for_lookup,,}"
  if [[ "$_enable_for_lookup" == "true" ]]; then
    diff_lines_file=$(mktemp_tracked /tmp/diff-new-lines-XXXXXXXX.txt)
    parse_diff_new_lines "$DIFF_FILE" > "$diff_lines_file"
  fi

  # Partition findings into inline (valid diff line) and body (everything else)
  local inline_comments="[]"
  local body_findings=""
  # Structured accumulator for body findings — used to assign stable IDs.
  local body_findings_ndjson_file
  body_findings_ndjson_file=$(mktemp_tracked /tmp/body-findings-ndjson-XXXXXXXX.ndjson)
  local inline_count=0
  local max_inline
  local _raw_mi="${AI_MAX_INLINE:-25}"
  if [[ "$_raw_mi" =~ ^[0-9]+$ ]]; then
    max_inline="$_raw_mi"
  else
    echo "WARNING: AI_MAX_INLINE='${_raw_mi}' is invalid; using default 25." >&2
    max_inline=25
  fi

  # Extract findings as newline-delimited JSON objects for safe iteration
  # (avoids seq 0 $((total-1)) which breaks on BSD when total=0)
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

    # Validate line is a positive integer (LLM may return non-numeric values)
    if ! [[ "$line" =~ ^[0-9]+$ ]]; then
      echo "WARNING: Skipping finding with non-numeric line: ${file}:${line}" >&2
      # Accumulate structured body-finding for ID assignment (rendered later).
      printf '%s\n' "$(jq -cn \
        --arg sev "$severity" --arg src "${source_tag:1:-1}" \
        --arg file "$file" --arg line "$line" \
        --arg text "$finding" --arg rem "$remediation" \
        --arg loc_note "" \
        '{severity:$sev, source:$src, file:$file, line:$line, finding:$text, remediation:$rem, loc_note:$loc_note}')" \
        >> "$body_findings_ndjson_file"
      continue
    fi

    # Suggestion handling — gated on AI_ENABLE_SUGGESTIONS (case-insensitive).
    # When disabled, suggested_code and start_line are ignored even if agents emit them.
    local _enable_suggestions_lc="${AI_ENABLE_SUGGESTIONS:-true}"
    _enable_suggestions_lc="${_enable_suggestions_lc,,}"
    if [[ "$_enable_suggestions_lc" != "true" ]]; then
      suggested_code=""
      start_line=""
    fi

    # Validate start_line: must be a positive integer (no leading zeros, no 0) and <= line.
    # Leading zeros would trigger bash octal interpretation in arithmetic contexts.
    # Drop suggestion on failure; the finding itself still posts with natural-language remediation.
    if [[ -n "$start_line" ]]; then
      if ! [[ "$start_line" =~ ^[1-9][0-9]*$ ]] || [[ "$start_line" -gt "$line" ]]; then
        echo "WARNING: Invalid start_line='${start_line}' for ${file}:${line}; dropping suggestion." >&2
        start_line=""
        suggested_code=""
      fi
    fi

    # Bound multi-line suggestion ranges to prevent malformed LLM output (e.g., line=99999999)
    # from hanging the workflow in a grep-per-iteration loop. 100 lines is generous for a
    # single suggestion — anything larger is almost certainly a hallucinated line number.
    local MAX_SUGGESTION_RANGE=100
    if [[ -n "$suggested_code" && -n "$start_line" && "$start_line" != "$line" ]]; then
      if (( line - start_line + 1 > MAX_SUGGESTION_RANGE )); then
        echo "WARNING: Suggestion range ${file}:${start_line}-${line} exceeds max ${MAX_SUGGESTION_RANGE} lines; dropping suggestion." >&2
        suggested_code=""
        start_line=""
      fi
    fi

    # Reject suggested_code containing triple-backticks. An LLM-emitted ``` would
    # close the ```suggestion fence early and let an attacker (via prompt injection
    # in PR content) inject arbitrary markdown into the review comment.
    if [[ -n "$suggested_code" && "$suggested_code" == *'```'* ]]; then
      echo "WARNING: suggested_code for ${file}:${line} contains triple-backticks; dropping suggestion to prevent fence escape." >&2
      suggested_code=""
      start_line=""
    fi

    # Validate multi-line suggestion range: every line in start_line..line must
    # appear in the diff's new-file side (added OR context lines, not deleted).
    # Defense-in-depth: the range-check block is only reachable when
    # AI_ENABLE_SUGGESTIONS=true (earlier gate clears suggested_code otherwise),
    # and diff_lines_file is always populated under that same condition. But if
    # the two gates ever drift, an empty "$diff_lines_file" would cause grep to
    # read from stdin and hang. If diff_lines_file is empty, drop the suggestion
    # rather than skip validation.
    if [[ -n "$suggested_code" && -n "$start_line" && "$start_line" != "$line" ]]; then
      if [[ -z "$diff_lines_file" ]]; then
        echo "WARNING: diff_lines_file unset for ${file}:${line} multi-line suggestion (AI_ENABLE_SUGGESTIONS drift?); dropping suggestion." >&2
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

    # Check if this line is a valid inline comment target (whole-line match)
    if grep -qxF "${file}:${line}" "$valid_lines_file" && [[ "$inline_count" -lt "$max_inline" ]]; then
      local comment_body
      comment_body="$(severity_icon "$severity") **[${severity}]** ${source_tag} ${finding}"
      if [[ -n "$remediation" ]]; then
        comment_body="${comment_body}

**Remediation:** ${remediation}"
      fi
      if [[ -n "$suggested_code" ]]; then
        comment_body="${comment_body}

\`\`\`suggestion
${suggested_code}
\`\`\`"
      fi

      if [[ -n "$start_line" && "$start_line" != "$line" ]]; then
        inline_comments=$(echo "$inline_comments" | jq \
          --arg path "$file" \
          --argjson line "$line" \
          --argjson start_line "$start_line" \
          --arg body "$comment_body" \
          '. + [{"path": $path, "line": $line, "start_line": $start_line, "body": $body}]')
      else
        inline_comments=$(echo "$inline_comments" | jq \
          --arg path "$file" \
          --argjson line "$line" \
          --arg body "$comment_body" \
          '. + [{"path": $path, "line": $line, "body": $body}]')
      fi
      inline_count=$((inline_count + 1))
    else
      # Append to review body. If the line isn't in the diff (as opposed to the
      # inline cap being hit), add a note so reviewers know the location may be
      # approximate (LLMs sometimes report lines from the full file context).
      local loc_note=""
      local drop_reason=""
      if ! grep -qxF "${file}:${line}" "$valid_lines_file"; then
        loc_note=" *(line not in diff)*"
        drop_reason="line not in diff"
      else
        drop_reason="inline cap of ${max_inline} reached"
      fi
      # When a suggestion was attached to this finding, log why it did not render
      # inline so operators can triage "hallucinated line" vs "capacity limit".
      if [[ -n "$suggested_code" ]]; then
        echo "WARNING: Suggestion for ${file}:${line} not rendered inline (${drop_reason}); rendering as code fence in review body instead." >&2
      fi
      # Accumulate structured body-finding for ID assignment (rendered later).
      printf '%s\n' "$(jq -cn \
        --arg sev "$severity" --arg src "${source_tag:1:-1}" \
        --arg file "$file" --arg line "$line" \
        --arg text "$finding" --arg rem "$remediation" \
        --arg loc_note "$loc_note" --arg sugg "$suggested_code" \
        '{severity:$sev, source:$src, file:$file, line:$line, finding:$text, remediation:$rem, loc_note:$loc_note, suggested_code:$sugg}')" \
        >> "$body_findings_ndjson_file"
    fi
  done <<< "$findings_ndjson"

  # Assign stable per-PR IDs to body findings and render them.
  if [[ -s "$body_findings_ndjson_file" ]]; then
    # Fetch prior bot review bodies for ID reconstruction.
    local prior_bodies_file
    prior_bodies_file=$(mktemp_tracked /tmp/prior-review-bodies-XXXXXXXX.txt)
    # Each line in prior_bodies_file is one review body with \n replaced by literal \n.
    gh api "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews?per_page=100" \
      --jq '.[] | select(.user.login == "github-actions[bot]") |
             select(.state == "CHANGES_REQUESTED" or .state == "COMMENTED") |
             select(.body | contains("### Findings not attached to specific lines")) |
             .body | gsub("\n"; "\\n")' \
      >> "$prior_bodies_file" 2>/dev/null || true

    local id_map_file next_id_file
    id_map_file=$(mktemp_tracked /tmp/finding-id-map-XXXXXXXX.tsv)
    next_id_file=$(mktemp_tracked /tmp/finding-next-id-XXXXXXXX.txt)
    finding_ids_build_map "$prior_bodies_file" "$id_map_file"
    local max_id
    max_id=$(finding_ids_max_from_bodies "$prior_bodies_file")
    echo $((max_id + 1)) > "$next_id_file"

    # Render each body finding with its assigned ID.
    while IFS= read -r bf_obj; do
      [[ -z "$bf_obj" ]] && continue
      local bf_sev bf_src bf_file bf_line bf_text bf_rem bf_loc_note bf_sugg
      bf_sev=$(printf '%s' "$bf_obj"   | jq -r '.severity')
      bf_src=$(printf '%s' "$bf_obj"   | jq -r '.source')
      bf_file=$(printf '%s' "$bf_obj"  | jq -r '.file')
      bf_line=$(printf '%s' "$bf_obj"  | jq -r '.line')
      bf_text=$(printf '%s' "$bf_obj"  | jq -r '.finding')
      bf_rem=$(printf '%s' "$bf_obj"   | jq -r '.remediation')
      bf_loc_note=$(printf '%s' "$bf_obj" | jq -r '.loc_note')
      bf_sugg=$(printf '%s' "$bf_obj"  | jq -r '.suggested_code')

      local bf_src_tag="[${bf_src}]"
      local bf_id
      bf_id=$(finding_ids_get "$bf_src" "$bf_file" "$bf_line" "$bf_text" "$id_map_file" "$next_id_file")

      body_findings="${body_findings}
$(format_body_finding "$bf_sev" "$bf_src_tag" "$bf_text" "${bf_file}:${bf_line}" "$bf_loc_note" "$bf_rem" "$bf_sugg" "$bf_id")"
    done < "$body_findings_ndjson_file"
  fi

  # Determine overall risk and review event from highest severity found
  #   No findings          → APPROVE
  #   Medium/Low findings  → APPROVE  (informational findings noted in review body)
  #   High/Critical        → REQUEST_CHANGES (blocking)
  local overall_risk finding_total review_event
  finding_total=$(echo "$findings_json" | jq 'length')

  # If key agents failed, never APPROVE — the review may be incomplete.
  # AI_REVIEW_FAILED_AGENTS is a colon-separated list passed from review.sh via env.
  local failed_agents_env="${AI_REVIEW_FAILED_AGENTS:-}"

  if [[ "$finding_total" -eq 0 ]]; then
    if [[ -n "$failed_agents_env" ]]; then
      overall_risk="Unknown"
      review_event="COMMENT"
    else
      overall_risk="None"
      review_event="APPROVE"
    fi
  elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "critical")' > /dev/null 2>&1; then
    overall_risk="Critical"
    review_event="REQUEST_CHANGES"
  elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "high")' > /dev/null 2>&1; then
    overall_risk="High"
    review_event="REQUEST_CHANGES"
  elif echo "$findings_json" | jq -e '.[] | select((.severity | ascii_downcase) == "medium")' > /dev/null 2>&1; then
    overall_risk="Medium"
    review_event="APPROVE"
  else
    overall_risk="Low"
    review_event="APPROVE"
  fi

  # Build review body
  local review_body
  if [[ "$review_event" == "APPROVE" ]]; then
    local approve_token_table=""
    if [[ -n "$TOKEN_TABLE_FILE" && -s "$TOKEN_TABLE_FILE" ]]; then
      approve_token_table=$(cat "$TOKEN_TABLE_FILE")
    fi
    if [[ "$finding_total" -eq 0 ]]; then
      review_body="## AI Review: Approved

No findings above the confidence threshold. The changes look good."
    else
      review_body="## AI Review: Approved

$(severity_icon "$overall_risk") **Overall Risk:** ${overall_risk} | **Findings:** ${finding_total} (${inline_count} inline)

No Critical or High findings. The changes look good — Medium/Low findings are informational only."
      if [[ -n "$body_findings" ]]; then
        review_body="${review_body}

### Findings (informational)
${body_findings}"
      fi
    fi
    if [[ -n "$approve_token_table" ]]; then
      review_body="${review_body}

${approve_token_table}"
    fi
    review_body="${review_body}

---
*AI Review — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
  elif [[ "$review_event" == "COMMENT" && "$overall_risk" == "Unknown" ]]; then
    local token_table=""
    if [[ -n "$TOKEN_TABLE_FILE" && -s "$TOKEN_TABLE_FILE" ]]; then
      token_table=$(cat "$TOKEN_TABLE_FILE")
    fi
    review_body="## AI Review: Incomplete

No findings above the confidence threshold, but one or more agents failed: ${failed_agents_env//:/, }

The review may be incomplete. Please verify manually or re-run the review."
    if [[ -n "$token_table" ]]; then
      review_body="${review_body}

${token_table}"
    fi
    review_body="${review_body}

---
*AI Review — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
  else
    review_body="## AI Review Findings

$(severity_icon "$overall_risk") **Overall Risk:** ${overall_risk} | **Findings:** ${finding_total} (${inline_count} inline)"

    if [[ -n "$body_findings" ]]; then
      review_body="${review_body}

### Findings not attached to specific lines
${body_findings}"
    elif [[ "$inline_count" -gt 0 ]]; then
      review_body="${review_body}

All findings are attached as inline comments."
    fi

    # Append token usage table if provided
    if [[ -n "$TOKEN_TABLE_FILE" && -s "$TOKEN_TABLE_FILE" ]]; then
      local token_table
      token_table=$(cat "$TOKEN_TABLE_FILE")
      review_body="${review_body}

${token_table}"
    fi

    review_body="${review_body}

---
*AI Review — generated by [ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
  fi

  # Append the "Prompt for AI agents" block when findings exist.
  # Placed after the footer so it doesn't break the review body layout;
  # the collapsible block is a utility section for copy-pasting into AI tools.
  if [[ "$finding_total" -gt 0 ]]; then
    agent_prompt=$(build_agent_prompt "$findings_json")
    if [[ -n "$agent_prompt" ]]; then
      review_body="${review_body}

${agent_prompt}"
    fi
  fi

  # Truncate review body to stay within GitHub's 65,536 char API limit
  review_body=$(truncate_body "$review_body")

  # GitHub does not allow inline comments on an APPROVE review.
  # When approving with findings present, post them as a COMMENT review first,
  # then post the APPROVE review separately (body only, no inline comments).
  if [[ "$review_event" == "APPROVE" && "$inline_count" -gt 0 ]]; then
    local comment_body findings_json_for_comment
    comment_body="$(severity_icon "$overall_risk") **Overall Risk:** ${overall_risk} | **Findings:** ${finding_total} (${inline_count} inline)"
    if [[ -n "$body_findings" ]]; then
      comment_body="${comment_body}

### Findings (informational)
${body_findings}"
    fi
    if [[ -n "$agent_prompt" ]]; then
      comment_body="${comment_body}

${agent_prompt}"
    fi
    findings_json_for_comment=$(jq -n \
      --arg body "$comment_body" \
      --arg commit_id "$HEAD_SHA" \
      --argjson comments "$inline_comments" \
      '{body: $body, event: "COMMENT", commit_id: $commit_id, comments: $comments}')
    local findings_post_result
    if findings_post_result=$(echo "$findings_json_for_comment" | gh_api_retry api "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews" \
      --method POST \
      --input - 2>&1); then
      echo "Posted inline findings as COMMENT review before APPROVE." >&2
    else
      echo "WARNING: Failed to post inline findings as COMMENT review before APPROVE: ${findings_post_result}" >&2
    fi
    # Clear inline comments from the APPROVE review — they were posted above
    inline_comments="[]"
    inline_count=0
  elif [[ "$review_event" == "APPROVE" ]]; then
    inline_comments="[]"
    inline_count=0
  fi

  # Build the review request JSON with commit_id to anchor inline comments
  local review_json
  review_json=$(jq -n \
    --arg body "$review_body" \
    --arg event "$review_event" \
    --arg commit_id "$HEAD_SHA" \
    --argjson comments "$inline_comments" \
    '{body: $body, event: $event, commit_id: $commit_id, comments: $comments}')

  # Build a fallback body that inlines the findings as body content, used
  # when every review-POST attempt fails and we degrade to a plain PR
  # comment (which does not support inline anchoring). Without this, the
  # finding text lives only in the inline_comments JSON and would be lost.
  # Use --argjson to normalize literal newlines in the JSON (jq writes
  # strings with embedded raw LF, which stdin parsing rejects but --argjson
  # accepts).
  local fallback_body="$review_body"
  if [[ "$inline_count" -gt 0 ]]; then
    local inline_rendered
    inline_rendered=$(jq -r --argjson comments "$inline_comments" '
      $comments[] |
      "- " + ((.body // "") | gsub("\n"; "\n  "))
    ' <<<'null')
    if [[ -n "$inline_rendered" ]]; then
      # Insert the rendered findings after the "inline comments" placeholder
      # (or at the head if that placeholder was not used).
      local inline_section
      inline_section="### Findings (inline anchoring unavailable)
${inline_rendered}"
      if [[ "$fallback_body" == *"All findings are attached as inline comments."* ]]; then
        fallback_body="${fallback_body//All findings are attached as inline comments./$inline_section}"
      else
        # Splice after the first "---" separator (below the summary),
        # otherwise append before the footer by inserting after the Overall Risk line.
        fallback_body=$(printf '%s' "$fallback_body" | awk -v section="$inline_section" '
          !inserted && /^\*\*Overall Risk:\*\*/ { print; print ""; print section; inserted=1; next }
          { print }
        ')
      fi
    fi
  fi

  echo "Posting review (${review_event}) with ${inline_count} inline comments..." >&2

  local review_result
  review_result=$(echo "$review_json" | gh_api_retry api "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews" \
    --method POST \
    --input - 2>&1) || {
    echo "WARNING: Failed to post ${review_event} review: ${review_result}" >&2

    # If REQUEST_CHANGES or APPROVE failed (e.g. GITHUB_TOKEN can't approve/block
    # its own PR author's work), retry as COMMENT
    if [[ "$review_event" == "REQUEST_CHANGES" || "$review_event" == "APPROVE" ]]; then
      echo "Retrying as COMMENT review..." >&2
      review_json=$(echo "$review_json" | jq '.event = "COMMENT"')
      review_result=$(echo "$review_json" | gh_api_retry api "repos/${OWNER}/${REPO}/pulls/${PR_NUMBER}/reviews" \
        --method POST \
        --input - 2>&1) || {
        echo "ERROR: COMMENT review also failed: ${review_result}" >&2
        echo "Falling back to posting as a PR comment..." >&2
        if gh_api_retry api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
          --method POST \
          --field body="${fallback_body}" > /dev/null 2>&1; then
          echo "Review posted as PR comment (${review_event} → COMMENT both unavailable) to PR #${PR_NUMBER}." >&2
          return 0
        fi
        # Reaches here only if the PR comment fallback above also failed.
        echo "ERROR: All three posting attempts failed (${review_event} → COMMENT → PR comment)." >&2
        return 1
      }
      echo "Review posted as COMMENT (${review_event} unavailable) to PR #${PR_NUMBER}." >&2
      return 0
    fi

    # Initial COMMENT review failed — fall back to regular PR comment
    echo "Falling back to posting as a PR comment..." >&2
    if gh_api_retry api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
      --method POST \
      --field body="${fallback_body}" > /dev/null 2>&1; then
      echo "Review posted as PR comment (${review_event} unavailable) to PR #${PR_NUMBER}." >&2
      return 0
    fi
    # Reaches here only if the PR comment fallback above also failed.
    echo "ERROR: All three posting attempts failed (${review_event} → COMMENT → PR comment)." >&2
    return 1
  }

  echo "Review posted (${review_event}) to PR #${PR_NUMBER}: ${inline_count} inline, overflow in body." >&2
}

# ---------------------------------------------------------------------------
# Advance the SHA watermark in the existing summary comment so that the next
# incremental review diffs from this HEAD, not the original first-review SHA.
# Only called when post_summary succeeded — if the summary comment was not posted,
# there is no existing comment to patch, so the update is skipped.
# ---------------------------------------------------------------------------
update_sha_marker() {
  local existing_comment_id existing_body
  existing_comment_id=$(gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
    --paginate \
    --jq ".[] | select(.body | contains(\"${MARKER_PREFIX}\")) | .id" \
    2>/dev/null | head -1) || true

  if [[ -z "$existing_comment_id" ]]; then
    echo "No existing summary comment found; SHA marker not updated." >&2
    return 0
  fi

  existing_body=$(gh api "repos/${OWNER}/${REPO}/issues/comments/${existing_comment_id}" \
    --jq '.body' 2>/dev/null) || {
    echo "WARNING: Could not fetch summary comment body for SHA update." >&2
    return 0
  }

  # Replace the sha= value in the marker line, preserving the rest of the comment
  local updated_body
  updated_body=$(echo "$existing_body" | sed "s|${MARKER_PREFIX} sha=[0-9a-f]* -->|${MARKER_PREFIX} sha=${HEAD_SHA} -->|")

  if [[ "$updated_body" == "$existing_body" ]]; then
    echo "SHA marker already at ${HEAD_SHA}; no update needed." >&2
    return 0
  fi

  gh_api_retry api "repos/${OWNER}/${REPO}/issues/comments/${existing_comment_id}" \
    --method PATCH \
    --field body="$updated_body" > /dev/null || {
    echo "WARNING: Failed to update SHA marker in summary comment." >&2
    return 0
  }

  echo "SHA marker advanced to ${HEAD_SHA}." >&2
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

resolve_stale_threads
dismiss_stale_reviews
summary_ok=true
post_summary || {
  echo "WARNING: Summary posting failed; continuing to post findings. The next run will fall back to a full PR diff." >&2
  summary_ok=false
}
post_findings || exit 1
if [[ "$summary_ok" == "true" ]]; then
  update_sha_marker
else
  echo "Skipping SHA marker update — summary comment was not posted." >&2
fi
