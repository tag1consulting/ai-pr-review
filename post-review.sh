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

$(echo "$findings_json" | jq -r '
  sort_by(
    if (.severity | ascii_downcase) == "critical" then 0
    elif (.severity | ascii_downcase) == "high" then 1
    elif (.severity | ascii_downcase) == "medium" then 2
    else 3 end
  ) | .[] |
  "- **[\(.severity)]** `\(.file // "unknown"):\(.line // "?")` — \(.finding)\n  **Remediation:** \(.remediation // "N/A")\n"
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

# Temp files — cleaned up on exit
TMPFILES=()
cleanup() {
  rm -f "${TMPFILES[@]}" 2>/dev/null || true
}
trap cleanup EXIT

mktemp_tracked() {
  local f
  f=$(mktemp "$@")
  TMPFILES+=("$f")
  echo "$f"
}

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
  review_ids=$(echo "$reviews_json" | jq -r '.[].id' 2>/dev/null) || true

  if [[ -z "$review_ids" ]]; then
    echo "No stale CHANGES_REQUESTED reviews to dismiss." >&2
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
  local truncated_summary
  truncated_summary=$(truncate_body "${summary}")
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
      echo "ERROR: Posted summary comment but could not capture its ID; skipping cleanup." >&2
      kept_comment_id=""
    else
      kept_comment_id="$new_comment_id"
    fi
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
  local duplicate_ids
  duplicate_ids=$(gh api "repos/${OWNER}/${REPO}/issues/${PR_NUMBER}/comments" \
    --paginate \
    --jq ".[] | select(.body | contains(\"${MARKER_PREFIX}\")) | .id" \
    2>/dev/null | grep -v "^${kept_id}$") || true
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
# Parse diff hunks to determine valid inline comment lines
# ---------------------------------------------------------------------------
# Builds a lookup of file:line pairs that are valid targets for inline comments.
# Only lines that appear in the "+" side of diff hunks are valid.
parse_valid_lines() {
  local diff_file="$1"
  local current_file=""
  local new_line=0

  while IFS= read -r line; do
    if [[ "$line" =~ ^diff\ --git\ a/(.+)\ b/(.+) ]]; then
      current_file="${BASH_REMATCH[2]}"
      new_line=0
    elif [[ "$line" =~ ^\+\+\+\  || "$line" =~ ^---\  ]]; then
      # Skip diff file headers (+++ b/file, --- a/file) — never treat as content
      continue
    elif [[ "$line" =~ ^@@\ -[0-9]+(,[0-9]+)?\ \+([0-9]+)(,[0-9]+)?\ @@ ]]; then
      new_line="${BASH_REMATCH[2]}"
    elif [[ -n "$current_file" && "$new_line" -gt 0 ]]; then
      if [[ "$line" =~ ^\+ ]]; then
        echo "${current_file}:${new_line}"
        new_line=$((new_line + 1))
      elif [[ "$line" =~ ^- ]]; then
        : # deleted line — don't increment new_line
      elif [[ "$line" =~ ^\\ ]]; then
        : # "\ No newline at end of file" — don't increment new_line
      else
        new_line=$((new_line + 1))
      fi
    fi
  done < "$diff_file"
}

# ---------------------------------------------------------------------------
# Map severity level to a color-coded icon for visual scanning.
# Uses distinct shapes in addition to color for accessibility.
# ---------------------------------------------------------------------------
severity_icon() {
  case "${1,,}" in
    critical) echo "❌" ;;
    high)     echo "🚨" ;;
    medium)   echo "🔶" ;;
    low)      echo "💬" ;;
    *)        echo "⚪" ;;
  esac
}

# ---------------------------------------------------------------------------
# Post Block B: Findings as a pull request review with inline comments
# ---------------------------------------------------------------------------
post_findings() {
  local findings
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

  # Partition findings into inline (valid diff line) and body (everything else)
  local inline_comments="[]"
  local body_findings=""
  local inline_count=0
  local max_inline=25

  # Extract findings as newline-delimited JSON objects for safe iteration
  # (avoids seq 0 $((total-1)) which breaks on BSD when total=0)
  local findings_ndjson
  findings_ndjson=$(echo "$findings_json" | jq -c '.[]' 2>/dev/null || true)

  while IFS= read -r finding_obj; do
    [[ -z "$finding_obj" ]] && continue
    local file line severity finding remediation
    file=$(echo "$finding_obj" | jq -r '.file // empty')
    line=$(echo "$finding_obj" | jq -r '.line // empty')
    severity=$(echo "$finding_obj" | jq -r '.severity // "Medium"')
    finding=$(echo "$finding_obj" | jq -r '.finding // empty')
    remediation=$(echo "$finding_obj" | jq -r '.remediation // empty')

    if [[ -z "$file" || -z "$line" || -z "$finding" ]]; then
      continue
    fi

    # Validate line is a positive integer (LLM may return non-numeric values)
    if ! [[ "$line" =~ ^[0-9]+$ ]]; then
      echo "WARNING: Skipping finding with non-numeric line: ${file}:${line}" >&2
      body_findings="${body_findings}
- $(severity_icon "$severity") **[${severity}]** ${finding} — \`${file}:${line}\`"
      continue
    fi

    # Check if this line is a valid inline comment target (whole-line match)
    if grep -qxF "${file}:${line}" "$valid_lines_file" && [[ "$inline_count" -lt "$max_inline" ]]; then
      local comment_body
      comment_body="$(severity_icon "$severity") **[${severity}]** ${finding}"
      if [[ -n "$remediation" ]]; then
        comment_body="${comment_body}

**Remediation:** ${remediation}"
      fi

      inline_comments=$(echo "$inline_comments" | jq \
        --arg path "$file" \
        --argjson line "$line" \
        --arg body "$comment_body" \
        '. + [{"path": $path, "line": $line, "body": $body}]')
      inline_count=$((inline_count + 1))
    else
      # Append to review body. If the line isn't in the diff (as opposed to the
      # inline cap being hit), add a note so reviewers know the location may be
      # approximate (LLMs sometimes report lines from the full file context).
      local loc_note=""
      if ! grep -qxF "${file}:${line}" "$valid_lines_file"; then
        loc_note=" *(line not in diff)*"
      fi
      body_findings="${body_findings}
- $(severity_icon "$severity") **[${severity}]** ${finding} — \`${file}:${line}\`${loc_note}"
    fi
  done <<< "$findings_ndjson"

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
          --field body="${review_body}" > /dev/null 2>&1; then
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
      --field body="${review_body}" > /dev/null 2>&1; then
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
