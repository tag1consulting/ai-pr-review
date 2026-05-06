#!/usr/bin/env bats
# Tests for pure functions in post-review-gitlab.sh.
#
# Covers duplicated helpers (severity_icon, format_source_tag, truncate_body,
# format_body_finding, build_agent_prompt, parse_valid_lines) plus 3-way
# parity tests that assert all three provider implementations (GitHub,
# Bitbucket, GitLab) produce identical output for shared fixtures — the
# drift-detection tripwire for the sibling-script pattern.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" severity_icon
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" format_source_tag
}

# ---------------------------------------------------------------------------
# severity_icon
# ---------------------------------------------------------------------------

@test "gitlab severity_icon: critical -> cross mark" {
  run severity_icon "critical"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "gitlab severity_icon: Critical (mixed case) -> cross mark" {
  run severity_icon "Critical"
  [ "$status" -eq 0 ]
  [ "$output" = "❌" ]
}

@test "gitlab severity_icon: high -> siren" {
  run severity_icon "high"
  [ "$output" = "🚨" ]
}

@test "gitlab severity_icon: medium -> orange diamond" {
  run severity_icon "medium"
  [ "$output" = "🔶" ]
}

@test "gitlab severity_icon: low -> speech bubble" {
  run severity_icon "low"
  [ "$output" = "💬" ]
}

@test "gitlab severity_icon: unknown -> white circle" {
  run severity_icon "info"
  [ "$output" = "⚪" ]
}

# ---------------------------------------------------------------------------
# format_source_tag
# ---------------------------------------------------------------------------

@test "gitlab format_source_tag: single source" {
  run format_source_tag '{"source":"code-reviewer"}'
  [ "$status" -eq 0 ]
  [ "$output" = "[code-reviewer]" ]
}

@test "gitlab format_source_tag: sources array with one entry" {
  run format_source_tag '{"sources":["shellcheck"]}'
  [ "$output" = "[shellcheck]" ]
}

@test "gitlab format_source_tag: multiple sources" {
  run format_source_tag '{"sources":["code-reviewer","semgrep","shellcheck"]}'
  [ "$output" = "[code-reviewer] *(also flagged by: semgrep,shellcheck)*" ]
}

@test "gitlab format_source_tag: missing both fields -> unknown" {
  run format_source_tag '{}'
  [ "$output" = "[unknown]" ]
}

# ---------------------------------------------------------------------------
# truncate_body — GitLab uses MAX_BODY_SIZE=250000
# ---------------------------------------------------------------------------

@test "gitlab truncate_body: short body is returned as-is" {
  MAX_BODY_SIZE=250000
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" truncate_body
  run truncate_body "hello world"
  [ "$status" -eq 0 ]
  [ "$output" = "hello world" ]
}

@test "gitlab truncate_body: body over 250000 bytes is truncated with notice" {
  MAX_BODY_SIZE=250000
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" truncate_body
  local long
  long=$(printf 'a%.0s' $(seq 1 260000))
  run truncate_body "$long"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  [[ "$output" == *"GitLab"* ]]
  local out_bytes
  out_bytes=$(printf '%s' "$output" | wc -c)
  [ "$out_bytes" -lt 250500 ]
}

@test "gitlab truncate_body: exactly at cap is returned as-is" {
  MAX_BODY_SIZE=250000
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" truncate_body
  local at_cap
  at_cap=$(printf 'a%.0s' $(seq 1 250000))
  run truncate_body "$at_cap"
  [ "$status" -eq 0 ]
  [[ "$output" != *"truncated"* ]]
  local out_bytes
  out_bytes=$(printf '%s' "$output" | wc -c)
  [ "$out_bytes" -eq 250000 ]
}

@test "gitlab truncate_body: multi-byte UTF-8 body truncated to valid UTF-8" {
  MAX_BODY_SIZE=250000
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" truncate_body
  # '日' is 3 bytes; 84000 repetitions = 252000 bytes (over cap).
  local long_utf8
  long_utf8=$(printf '日%.0s' $(seq 1 84000))
  run truncate_body "$long_utf8"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Review output truncated"* ]]
  printf '%s' "$output" | iconv -f UTF-8 -t UTF-8 > /dev/null
}

# ---------------------------------------------------------------------------
# format_body_finding
# ---------------------------------------------------------------------------

@test "gitlab format_body_finding: basic finding with remediation" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" severity_icon
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" format_body_finding
  run format_body_finding "High" "[code-reviewer]" "SQL injection risk" "app.py:42" "" "Use parameterized queries"
  [ "$status" -eq 0 ]
  [[ "$output" == *"🚨"* ]]
  [[ "$output" == *"[High]"* ]]
  [[ "$output" == *"SQL injection risk"* ]]
  [[ "$output" == *"<details>"* ]]
  [[ "$output" == *"Remediation"* ]]
}

@test "gitlab format_body_finding: finding without remediation" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" severity_icon
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" format_body_finding
  run format_body_finding "Low" "[shellcheck]" "Unused variable" "script.sh:10" "" ""
  [ "$status" -eq 0 ]
  [[ "$output" == *"💬"* ]]
  [[ "$output" != *"<details>"* ]]
}

@test "gitlab format_body_finding: suggested_code with triple backticks is rejected" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" severity_icon
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" format_body_finding
  run format_body_finding "Medium" "[agent]" "Issue" "f.py:1" "" "Fix it" '```evil```'
  [ "$status" -eq 0 ]
  [[ "$output" != *'```evil```'* ]]
}

# ---------------------------------------------------------------------------
# build_agent_prompt
# ---------------------------------------------------------------------------

@test "gitlab build_agent_prompt: groups findings by file" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" build_agent_prompt
  local json='[
    {"file":"a.py","line":1,"finding":"issue A","remediation":"fix A"},
    {"file":"a.py","line":5,"finding":"issue B","remediation":"fix B"},
    {"file":"b.py","line":10,"finding":"issue C","remediation":""}
  ]'
  run build_agent_prompt "$json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"a.py"* ]]
  [[ "$output" == *"b.py"* ]]
  [[ "$output" == *"Prompt for AI agents"* ]]
}

@test "gitlab build_agent_prompt: empty findings returns nothing" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" build_agent_prompt
  run build_agent_prompt "[]"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# parse_valid_lines
# ---------------------------------------------------------------------------

@test "gitlab parse_valid_lines: extracts added lines from unified diff" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" parse_valid_lines
  local diff_file
  diff_file=$(mktemp)
  cat > "$diff_file" << 'DIFF'
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 unchanged
+added line
 context
+another added
DIFF
  run parse_valid_lines "$diff_file"
  rm -f "$diff_file"
  [ "$status" -eq 0 ]
  [[ "$output" == *"foo.py:2"* ]]
  [[ "$output" == *"foo.py:4"* ]]
  # Context lines should NOT appear (only + lines)
  [[ "$output" != *"foo.py:1"* ]]
  [[ "$output" != *"foo.py:3"* ]]
}

# ---------------------------------------------------------------------------
# classify_risk
# ---------------------------------------------------------------------------

@test "gitlab classify_risk: no findings no failures -> None|APPROVE" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" classify_risk
  unset AI_REVIEW_FAILED_AGENTS
  run classify_risk "[]"
  [ "$output" = "None|APPROVE" ]
}

@test "gitlab classify_risk: no findings with failures -> Unknown|COMMENT" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" classify_risk
  AI_REVIEW_FAILED_AGENTS="agent-a:agent-b"
  run classify_risk "[]"
  [ "$output" = "Unknown|COMMENT" ]
  unset AI_REVIEW_FAILED_AGENTS
}

@test "gitlab classify_risk: critical finding -> Critical|REQUEST_CHANGES" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" classify_risk
  run classify_risk '[{"severity":"Critical","file":"a.py","line":1,"finding":"x"}]'
  [ "$output" = "Critical|REQUEST_CHANGES" ]
}

@test "gitlab classify_risk: high finding -> High|REQUEST_CHANGES" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" classify_risk
  run classify_risk '[{"severity":"High","file":"a.py","line":1,"finding":"x"}]'
  [ "$output" = "High|REQUEST_CHANGES" ]
}

@test "gitlab classify_risk: medium finding -> Medium|APPROVE" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" classify_risk
  run classify_risk '[{"severity":"Medium","file":"a.py","line":1,"finding":"x"}]'
  [ "$output" = "Medium|APPROVE" ]
}

@test "gitlab classify_risk: low finding -> Low|APPROVE" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" classify_risk
  run classify_risk '[{"severity":"Low","file":"a.py","line":1,"finding":"x"}]'
  [ "$output" = "Low|APPROVE" ]
}

# ---------------------------------------------------------------------------
# resolve_project_id — error paths
# ---------------------------------------------------------------------------

@test "gitlab resolve_project_id: no env vars set -> error exit" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  unset GITLAB_PROJECT_ID CI_PROJECT_ID CI_PROJECT_PATH GITHUB_REPOSITORY 2>/dev/null || true
  run resolve_project_id
  [ "$status" -ne 0 ]
  [[ "$output" == *"Cannot resolve GitLab project ID"* ]]
}

@test "gitlab resolve_project_id: GITHUB_REPOSITORY with subgroups is accepted" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  unset GITLAB_PROJECT_ID CI_PROJECT_ID CI_PROJECT_PATH 2>/dev/null || true
  GITHUB_REPOSITORY="group/subgroup/project"
  resolve_project_id
  [ "$PROJECT_ID" = "group%2Fsubgroup%2Fproject" ]
}

@test "gitlab resolve_project_id: non-numeric GITLAB_PROJECT_ID falls through to CI_PROJECT_ID" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  GITLAB_PROJECT_ID="not-a-number"
  CI_PROJECT_ID="99999"
  unset CI_PROJECT_PATH GITHUB_REPOSITORY 2>/dev/null || true
  resolve_project_id
  [ "$PROJECT_ID" = "99999" ]
}

# ---------------------------------------------------------------------------
# resolve_project_id — positive paths (moved from above)
# ---------------------------------------------------------------------------

@test "gitlab resolve_project_id: explicit numeric GITLAB_PROJECT_ID" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  GITLAB_PROJECT_ID="12345"
  unset CI_PROJECT_ID CI_PROJECT_PATH GITHUB_REPOSITORY 2>/dev/null || true
  resolve_project_id
  [ "$PROJECT_ID" = "12345" ]
}

@test "gitlab resolve_project_id: falls back to CI_PROJECT_ID" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  unset GITLAB_PROJECT_ID 2>/dev/null || true
  CI_PROJECT_ID="67890"
  unset CI_PROJECT_PATH GITHUB_REPOSITORY 2>/dev/null || true
  resolve_project_id
  [ "$PROJECT_ID" = "67890" ]
}

@test "gitlab resolve_project_id: URL-encodes CI_PROJECT_PATH" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  unset GITLAB_PROJECT_ID CI_PROJECT_ID 2>/dev/null || true
  CI_PROJECT_PATH="mygroup/myproject"
  unset GITHUB_REPOSITORY 2>/dev/null || true
  resolve_project_id
  [ "$PROJECT_ID" = "mygroup%2Fmyproject" ]
}

@test "gitlab resolve_project_id: URL-encodes GITHUB_REPOSITORY fallback" {
  load_function "${PROJECT_ROOT}/post-review-gitlab.sh" resolve_project_id
  unset GITLAB_PROJECT_ID CI_PROJECT_ID CI_PROJECT_PATH 2>/dev/null || true
  GITHUB_REPOSITORY="ns/repo"
  resolve_project_id
  [ "$PROJECT_ID" = "ns%2Frepo" ]
}

# ===========================================================================
# 3-way parity tests: GitHub vs Bitbucket vs GitLab
# ===========================================================================

@test "parity: severity_icon identical across all three providers" {
  local inputs=("critical" "Critical" "high" "HIGH" "medium" "Medium" "low" "LOW" "info" "")
  local gh_fn bb_fn gl_fn
  gh_fn=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")
  for input in "${inputs[@]}"; do
    local gh_out bb_out gl_out
    gh_out=$(bash -c "${gh_fn}"'; severity_icon "$1"' _ "$input")
    bb_out=$(bash -c "${bb_fn}"'; severity_icon "$1"' _ "$input")
    gl_out=$(bash -c "${gl_fn}"'; severity_icon "$1"' _ "$input")
    [ "$gh_out" = "$bb_out" ] || { echo "drift gh/bb on input='${input}': gh='${gh_out}' bb='${bb_out}'" >&2; return 1; }
    [ "$gh_out" = "$gl_out" ] || { echo "drift gh/gl on input='${input}': gh='${gh_out}' gl='${gl_out}'" >&2; return 1; }
  done
}

@test "parity: format_source_tag identical across all three providers" {
  local fixtures=(
    '{"source":"code-reviewer"}'
    '{"sources":["a","b","c"]}'
    '{"sources":[]}'
    '{}'
    '{"source":"","sources":[]}'
    '{"source":"agent-a","sources":["agent-a"]}'
  )
  local gh_fn bb_fn gl_fn
  gh_fn=$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^format_source_tag\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")
  for fixture in "${fixtures[@]}"; do
    local gh_out bb_out gl_out
    gh_out=$(bash -c "${gh_fn}"'; format_source_tag "$1"' _ "$fixture")
    bb_out=$(bash -c "${bb_fn}"'; format_source_tag "$1"' _ "$fixture")
    gl_out=$(bash -c "${gl_fn}"'; format_source_tag "$1"' _ "$fixture")
    [ "$gh_out" = "$bb_out" ] || { echo "drift gh/bb on fixture='${fixture}': gh='${gh_out}' bb='${bb_out}'" >&2; return 1; }
    [ "$gh_out" = "$gl_out" ] || { echo "drift gh/gl on fixture='${fixture}': gh='${gh_out}' gl='${gl_out}'" >&2; return 1; }
  done
}

@test "parity: truncate_body short inputs pass through unchanged in all three scripts" {
  local fixtures=(
    "hello world"
    ""
  )
  local gh_fn bb_fn gl_fn
  gh_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")
  for fixture in "${fixtures[@]}"; do
    local gh_out bb_out gl_out
    gh_out=$(bash -c "${gh_fn}"'; truncate_body "$1"' _ "$fixture")
    bb_out=$(bash -c "${bb_fn}"'; truncate_body "$1"' _ "$fixture")
    gl_out=$(bash -c "${gl_fn}"'; truncate_body "$1"' _ "$fixture")
    [ "$gh_out" = "$bb_out" ] || { echo "drift gh/bb (len=${#fixture})" >&2; return 1; }
    [ "$gh_out" = "$gl_out" ] || { echo "drift gh/gl (len=${#fixture})" >&2; return 1; }
  done
}

@test "parity: truncate_body mid-range (33000 bytes): BB truncates, GH and GL pass through" {
  local mid_input
  mid_input=$(printf 'x%.0s' $(seq 1 33000))

  local gh_fn bb_fn gl_fn
  gh_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local gh_out bb_out gl_out
  gh_out=$(bash -c "${gh_fn}"'; truncate_body "$1"' _ "$mid_input")
  bb_out=$(bash -c "${bb_fn}"'; truncate_body "$1"' _ "$mid_input")
  gl_out=$(bash -c "${gl_fn}"'; truncate_body "$1"' _ "$mid_input")

  # GitHub (64000 cap): passes through
  [ "${#gh_out}" -eq 33000 ] || { echo "GitHub should pass through: got ${#gh_out}" >&2; return 1; }
  # Bitbucket (32000 cap): truncates
  [[ "$bb_out" == *"Review output truncated"* ]] || { echo "Bitbucket should truncate" >&2; return 1; }
  # GitLab (250000 cap): passes through
  [ "${#gl_out}" -eq 33000 ] || { echo "GitLab should pass through: got ${#gl_out}" >&2; return 1; }
  [[ "$gl_out" != *"truncated"* ]] || { echo "GitLab should not truncate 33000 bytes" >&2; return 1; }
}

@test "parity: truncate_body large (65000 bytes): BB and GH truncate, GL passes through" {
  local large_input
  large_input=$(printf 'x%.0s' $(seq 1 65000))

  local gh_fn bb_fn gl_fn
  gh_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^MAX_BODY_SIZE=/{print} /^truncate_body\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local gh_out bb_out gl_out
  gh_out=$(bash -c "${gh_fn}"'; truncate_body "$1"' _ "$large_input")
  bb_out=$(bash -c "${bb_fn}"'; truncate_body "$1"' _ "$large_input")
  gl_out=$(bash -c "${gl_fn}"'; truncate_body "$1"' _ "$large_input")

  # GitHub (64000 cap): truncates
  [[ "$gh_out" == *"Review output truncated"* ]] || { echo "GitHub should truncate" >&2; return 1; }
  # Bitbucket (32000 cap): truncates
  [[ "$bb_out" == *"Review output truncated"* ]] || { echo "Bitbucket should truncate" >&2; return 1; }
  # GitLab (250000 cap): passes through
  [ "${#gl_out}" -eq 65000 ] || { echo "GitLab should pass through: got ${#gl_out}" >&2; return 1; }
  [[ "$gl_out" != *"truncated"* ]] || { echo "GitLab should not truncate 65000 bytes" >&2; return 1; }
}

@test "parity: mktemp_tracked registers files for cleanup in all three scripts" {
  local gh_fn bb_fn gl_fn
  gh_fn=$(awk '/^mktemp_tracked\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  bb_fn=$(awk '/^mktemp_tracked\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^mktemp_tracked\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local gh_path_file bb_path_file gl_path_file
  gh_path_file=$(mktemp /tmp/parity-gh-pathfile-XXXXXXXX)
  bb_path_file=$(mktemp /tmp/parity-bb-pathfile-XXXXXXXX)
  gl_path_file=$(mktemp /tmp/parity-gl-pathfile-XXXXXXXX)
  # shellcheck disable=SC2064
  trap "rm -f '$gh_path_file' '$bb_path_file' '$gl_path_file'" EXIT

  local gh_result bb_result gl_result
  gh_result=$(bash -c '
    TMPFILES=()
    '"${gh_fn}"'
    mktemp_tracked /tmp/parity-gh-XXXXXXXX > "$1"
    f=$(cat "$1")
    [[ "${TMPFILES[0]}" == "$f" ]] && echo "ok:$f" || echo "not-registered:$f"
  ' _ "$gh_path_file")
  bb_result=$(bash -c '
    TMPFILES=()
    '"${bb_fn}"'
    mktemp_tracked /tmp/parity-bb-XXXXXXXX > "$1"
    f=$(cat "$1")
    [[ "${TMPFILES[0]}" == "$f" ]] && echo "ok:$f" || echo "not-registered:$f"
  ' _ "$bb_path_file")
  gl_result=$(bash -c '
    TMPFILES=()
    '"${gl_fn}"'
    mktemp_tracked /tmp/parity-gl-XXXXXXXX > "$1"
    f=$(cat "$1")
    [[ "${TMPFILES[0]}" == "$f" ]] && echo "ok:$f" || echo "not-registered:$f"
  ' _ "$gl_path_file")

  [[ "$gh_result" == ok:* ]] || { echo "GitHub mktemp_tracked did not register: ${gh_result}" >&2; return 1; }
  [[ "$bb_result" == ok:* ]] || { echo "Bitbucket mktemp_tracked did not register: ${bb_result}" >&2; return 1; }
  [[ "$gl_result" == ok:* ]] || { echo "GitLab mktemp_tracked did not register: ${gl_result}" >&2; return 1; }

  local gh_path bb_path gl_path
  gh_path="${gh_result#ok:}"
  bb_path="${bb_result#ok:}"
  gl_path="${gl_result#ok:}"
  rm -f "$gh_path" "$bb_path" "$gl_path"
}

@test "parity: format_body_finding identical between GitHub and GitLab" {
  local gh_fn gl_fn
  # Extract both severity_icon and format_body_finding from each script
  local gh_si gl_si
  gh_si=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  gl_si=$(awk '/^severity_icon\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")
  gh_fn=$(awk '/^format_body_finding\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  gl_fn=$(awk '/^format_body_finding\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local fixtures=(
    'High|[agent]|SQL injection|app.py:42||Use params|'
    'Low|[sc]|Unused var|s.sh:10|||'
    'Medium|[cr]|Issue|f.py:1||Fix it|echo hello'
  )
  for fixture in "${fixtures[@]}"; do
    IFS='|' read -r sev tag finding loc note rem code <<< "$fixture"
    local gh_out gl_out
    gh_out=$(bash -c "${gh_si}"$'\n'"${gh_fn}"'; format_body_finding "$@"' _ "$sev" "$tag" "$finding" "$loc" "$note" "$rem" "$code")
    gl_out=$(bash -c "${gl_si}"$'\n'"${gl_fn}"'; format_body_finding "$@"' _ "$sev" "$tag" "$finding" "$loc" "$note" "$rem" "$code")
    [ "$gh_out" = "$gl_out" ] || { echo "drift on fixture '${fixture}'" >&2; echo "GH: ${gh_out}" >&2; echo "GL: ${gl_out}" >&2; return 1; }
  done
}

@test "parity: build_agent_prompt identical between GitHub and GitLab" {
  local gh_fn gl_fn
  gh_fn=$(awk '/^build_agent_prompt\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  gl_fn=$(awk '/^build_agent_prompt\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local json='[
    {"file":"a.py","line":1,"finding":"issue A","remediation":"fix A"},
    {"file":"b.py","line":10,"finding":"issue B","remediation":""}
  ]'
  local gh_out gl_out
  gh_out=$(bash -c "${gh_fn}"'; build_agent_prompt "$1"' _ "$json")
  gl_out=$(bash -c "${gl_fn}"'; build_agent_prompt "$1"' _ "$json")
  [ "$gh_out" = "$gl_out" ] || { echo "drift in build_agent_prompt" >&2; return 1; }
}

@test "parity: parse_valid_lines identical between GitHub and GitLab" {
  local gh_fn gl_fn
  gh_fn=$(awk '/^parse_valid_lines\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  gl_fn=$(awk '/^parse_valid_lines\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local diff_file
  diff_file=$(mktemp)
  cat > "$diff_file" << 'DIFF'
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,5 @@
 context
+added
 more context
+another
 end
DIFF

  local gh_out gl_out
  gh_out=$(bash -c "${gh_fn}"'; parse_valid_lines "$1"' _ "$diff_file")
  gl_out=$(bash -c "${gl_fn}"'; parse_valid_lines "$1"' _ "$diff_file")
  rm -f "$diff_file"
  [ "$gh_out" = "$gl_out" ] || { echo "drift in parse_valid_lines: gh='${gh_out}' gl='${gl_out}'" >&2; return 1; }
}

@test "parity: parse_diff_new_lines identical between GitHub and GitLab" {
  local gh_fn gl_fn
  gh_fn=$(awk '/^parse_diff_new_lines\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review.sh")
  gl_fn=$(awk '/^parse_diff_new_lines\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local diff_file
  diff_file=$(mktemp)
  cat > "$diff_file" << 'DIFF'
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,4 +1,5 @@
 context
+added
 more context
-deleted
+replaced
 end
DIFF

  local gh_out gl_out
  gh_out=$(bash -c "${gh_fn}"'; parse_diff_new_lines "$1"' _ "$diff_file")
  gl_out=$(bash -c "${gl_fn}"'; parse_diff_new_lines "$1"' _ "$diff_file")
  rm -f "$diff_file"
  [ "$gh_out" = "$gl_out" ] || { echo "drift in parse_diff_new_lines: gh='${gh_out}' gl='${gl_out}'" >&2; return 1; }
}

@test "parity: classify_risk identical between Bitbucket and GitLab" {
  local bb_fn gl_fn
  bb_fn=$(awk '/^classify_risk\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-bitbucket.sh")
  gl_fn=$(awk '/^classify_risk\(\)/{f=1} f{print} f && /^}$/{exit}' "${PROJECT_ROOT}/post-review-gitlab.sh")

  local fixtures=(
    '[]'
    '[{"severity":"Critical","file":"a.py","line":1,"finding":"x"}]'
    '[{"severity":"High","file":"a.py","line":1,"finding":"x"}]'
    '[{"severity":"Medium","file":"a.py","line":1,"finding":"x"}]'
    '[{"severity":"Low","file":"a.py","line":1,"finding":"x"}]'
  )
  for fixture in "${fixtures[@]}"; do
    local bb_out gl_out
    bb_out=$(bash -c 'AI_REVIEW_FAILED_AGENTS=""; '"${bb_fn}"'; classify_risk "$1"' _ "$fixture")
    gl_out=$(bash -c 'AI_REVIEW_FAILED_AGENTS=""; '"${gl_fn}"'; classify_risk "$1"' _ "$fixture")
    [ "$bb_out" = "$gl_out" ] || { echo "drift on fixture='${fixture}': bb='${bb_out}' gl='${gl_out}'" >&2; return 1; }
  done
  # Also test with failed agents
  local bb_fail gl_fail
  bb_fail=$(bash -c 'AI_REVIEW_FAILED_AGENTS="agent-x"; '"${bb_fn}"'; classify_risk "[]"' _)
  gl_fail=$(bash -c 'AI_REVIEW_FAILED_AGENTS="agent-x"; '"${gl_fn}"'; classify_risk "[]"' _)
  [ "$bb_fail" = "$gl_fail" ] || { echo "drift on failed-agents: bb='${bb_fail}' gl='${gl_fail}'" >&2; return 1; }
}
