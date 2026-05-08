#!/usr/bin/env bash
# vcs/common.sh — shared helpers for post-review-{github,bitbucket,gitlab}.sh
#
# This file is sourced by all three post-review scripts. It exports pure
# helpers (no API calls, no provider-specific constants) plus temp-file
# lifecycle helpers. Functions that genuinely diverge per-provider
# (truncate_body, *_api, find_existing_summary_id, etc.) stay in their
# owning scripts.
#
# Contract: sourcing scripts MUST declare `TMPFILES=()` before sourcing
# this file, then `trap cleanup EXIT` afterwards. All three post-review
# scripts (and the bats tests that source common.sh) comply. No sentinel
# is declared here — an unguarded `TMPFILES=("${TMPFILES[@]:-}")` would
# create a `[0]=""` entry when TMPFILES is unset, which is incorrect.

# --- Temp-file lifecycle ---------------------------------------------------

mktemp_tracked() {
  local f
  f=$(mktemp "$@")
  TMPFILES+=("$f")
  echo "$f"
}

cleanup() {
  rm -f "${TMPFILES[@]}" 2>/dev/null || true
}

# --- Severity / risk helpers ----------------------------------------------

severity_icon() {
  case "${1,,}" in
    critical) echo "❌" ;;
    high)     echo "🚨" ;;
    medium)   echo "🔶" ;;
    low)      echo "💬" ;;
    *)        echo "⚪" ;;
  esac
}

# classify_risk: map findings JSON array to "<label>|<review-event>".
# Label is one of None/Low/Medium/High/Critical/Unknown. Review event is
# APPROVE/COMMENT/REQUEST_CHANGES. Providers that don't use the event
# (GitLab uses a different approval model) ignore the second field.
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

# --- Source attribution ----------------------------------------------------

# Given a finding object, emit the provenance tag rendered inside comments
# (e.g. "[security-reviewer]" or "[semgrep] *(also flagged by: code-reviewer)*").
# Prefers the deduplicated sources[] array; falls back to .source scalar.
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

# --- Diff parsing ----------------------------------------------------------

# parse_valid_lines: emit "<file>:<line>" for every ADDED line (+ prefix) in
# the unified diff at $1. Used to decide which findings can go inline on a
# review comment anchor.
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

# parse_diff_new_lines: emit "<file>:<line>" for every line present in the
# new file (both added AND context), used for multi-line suggestion range
# validation where context lines are acceptable anchors.
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
        : # deleted line — not in new file
      elif [[ "$line" =~ ^\\ ]]; then
        : # "\ No newline at end of file" marker
      else
        # Context line — present in new file at new_line
        echo "${current_file}:${new_line}"
        new_line=$((new_line + 1))
      fi
    fi
  done < "$diff_file"
}

# --- Rendering -------------------------------------------------------------

# format_body_finding: one bullet per finding, wrapping remediation +
# optional suggested_code in a collapsible <details> block. Used by the
# GitHub and GitLab summary-body renderers. Bitbucket uses a shorter
# render_findings_markdown that stays in post-review-bitbucket.sh
# (no <details> support).
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

# build_agent_prompt: emit a collapsible <details> block that copy-pastes
# into an AI coding assistant. Returns silently on empty input or jq
# failure. GitLab's safer jq_ok guard is adopted here over GitHub's
# implicit-continue pattern.
build_agent_prompt() {
  local findings_json="$1"
  local count
  count=$(echo "$findings_json" | jq 'length' 2>/dev/null || echo 0)
  [[ "$count" -eq 0 ]] && return

  local prompt_body
  local jq_ok=true
  prompt_body=$(echo "$findings_json" | jq -r '
    group_by(.file) | map(
      "In `\(.[0].file)`:" as $header |
      [$header] + [
        .[] |
        "- Around line \(.line // "?"): \(.finding)" +
          if (.remediation // "") != "" then ". " + .remediation else "" end
      ] | join("\n")
    ) | join("\n\n")
  ' 2>/dev/null) || jq_ok=false
  if [[ "$jq_ok" == "false" ]]; then
    echo "WARNING: build_agent_prompt: jq failed; agent prompt block will be omitted." >&2
    return
  fi

  [[ -z "$prompt_body" ]] && return

  printf '<details>\n<summary>🤖 Prompt for AI agents</summary>\n\n```\nVerify each finding against the current code and only fix it if needed.\n\n%s\n```\n\n</details>' "$prompt_body"
}
