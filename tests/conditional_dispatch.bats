#!/usr/bin/env bats
# Tests for detect_conditional_agent_triggers in lib/diff.sh.
# Verifies that diff-heuristic gates correctly set dispatch flags for
# architecture-reviewer, security-reviewer, and edge-case-hunter, plus
# the existing HAS_ERROR_PATTERNS flag for silent-failure-hunter.
#
# Each test:
#   1. Loads the function via load_function (no full-script sourcing).
#   2. Sets DIFF_FILE and CHANGED_FILES from fixture files.
#   3. Calls detect_conditional_agent_triggers.
#   4. Asserts the four boolean globals.

setup() {
  load test_helper
  FIXTURES_DIR="${PROJECT_ROOT}/tests/fixtures/gates"
  load_function "${PROJECT_ROOT}/lib/diff.sh" detect_conditional_agent_triggers
  DIFF_FILE=$(mktemp)
  CHANGED_FILES=""
  # Unset kill switches between tests to avoid cross-contamination.
  unset AI_DISABLE_GATE_ARCHITECTURE AI_DISABLE_GATE_SECURITY AI_DISABLE_GATE_EDGE_CASE
}

teardown() {
  rm -f "$DIFF_FILE"
}

# ---------------------------------------------------------------------------
# Docs-only: all three gated agents should be skipped
# ---------------------------------------------------------------------------

@test "docs-only diff: architecture-reviewer skipped" {
  cp "${FIXTURES_DIR}/docs_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/docs_only.files")
  detect_conditional_agent_triggers
  [ "$RUN_ARCHITECTURE_REVIEWER" = "false" ]
}

@test "docs-only diff: security-reviewer skipped" {
  cp "${FIXTURES_DIR}/docs_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/docs_only.files")
  detect_conditional_agent_triggers
  [ "$RUN_SECURITY_REVIEWER" = "false" ]
}

@test "docs-only diff: edge-case-hunter skipped" {
  cp "${FIXTURES_DIR}/docs_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/docs_only.files")
  detect_conditional_agent_triggers
  [ "$RUN_EDGE_CASE_HUNTER" = "false" ]
}

@test "docs-only diff: HAS_ERROR_PATTERNS remains 0" {
  cp "${FIXTURES_DIR}/docs_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/docs_only.files")
  detect_conditional_agent_triggers
  [ "$HAS_ERROR_PATTERNS" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Code diff with no security keywords: arch and edge run, security skipped
# ---------------------------------------------------------------------------

@test "code-no-security diff: architecture-reviewer runs" {
  cp "${FIXTURES_DIR}/code_no_security.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/code_no_security.files")
  detect_conditional_agent_triggers
  [ "$RUN_ARCHITECTURE_REVIEWER" = "true" ]
}

@test "code-no-security diff: security-reviewer skipped" {
  cp "${FIXTURES_DIR}/code_no_security.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/code_no_security.files")
  detect_conditional_agent_triggers
  [ "$RUN_SECURITY_REVIEWER" = "false" ]
}

@test "code-no-security diff: edge-case-hunter runs (has control flow)" {
  cp "${FIXTURES_DIR}/code_no_security.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/code_no_security.files")
  detect_conditional_agent_triggers
  [ "$RUN_EDGE_CASE_HUNTER" = "true" ]
}

# ---------------------------------------------------------------------------
# Security keyword present: security-reviewer runs
# ---------------------------------------------------------------------------

@test "security-keyword diff: security-reviewer runs" {
  cp "${FIXTURES_DIR}/security_keyword.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/security_keyword.files")
  detect_conditional_agent_triggers
  [ "$RUN_SECURITY_REVIEWER" = "true" ]
}

# ---------------------------------------------------------------------------
# Security-sensitive file path: security-reviewer runs even without keywords
# ---------------------------------------------------------------------------

@test "security-path diff: security-reviewer runs due to package.json" {
  cp "${FIXTURES_DIR}/security_path.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/security_path.files")
  detect_conditional_agent_triggers
  [ "$RUN_SECURITY_REVIEWER" = "true" ]
}

# ---------------------------------------------------------------------------
# No control flow in diff: edge-case-hunter skipped
# ---------------------------------------------------------------------------

@test "no-control-flow diff: edge-case-hunter skipped" {
  cp "${FIXTURES_DIR}/no_control_flow.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/no_control_flow.files")
  detect_conditional_agent_triggers
  [ "$RUN_EDGE_CASE_HUNTER" = "false" ]
}

@test "no-control-flow diff: architecture-reviewer runs (code file)" {
  cp "${FIXTURES_DIR}/no_control_flow.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/no_control_flow.files")
  detect_conditional_agent_triggers
  [ "$RUN_ARCHITECTURE_REVIEWER" = "true" ]
}

# ---------------------------------------------------------------------------
# .github/workflows/ only: architecture-reviewer runs (infra), not docs
# ---------------------------------------------------------------------------

@test "workflows-only diff: architecture-reviewer runs" {
  cp "${FIXTURES_DIR}/workflows_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/workflows_only.files")
  detect_conditional_agent_triggers
  [ "$RUN_ARCHITECTURE_REVIEWER" = "true" ]
}

@test "workflows-only diff: security-reviewer runs (workflow path triggers it)" {
  cp "${FIXTURES_DIR}/workflows_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/workflows_only.files")
  detect_conditional_agent_triggers
  [ "$RUN_SECURITY_REVIEWER" = "true" ]
}

# ---------------------------------------------------------------------------
# Mixed (docs + code): all agents run
# ---------------------------------------------------------------------------

@test "mixed diff: architecture-reviewer runs" {
  cp "${FIXTURES_DIR}/mixed.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/mixed.files")
  detect_conditional_agent_triggers
  [ "$RUN_ARCHITECTURE_REVIEWER" = "true" ]
}

@test "mixed diff: edge-case-hunter runs (has control flow)" {
  cp "${FIXTURES_DIR}/mixed.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/mixed.files")
  detect_conditional_agent_triggers
  [ "$RUN_EDGE_CASE_HUNTER" = "true" ]
}

# ---------------------------------------------------------------------------
# Kill switches override gate logic
# ---------------------------------------------------------------------------

@test "AI_DISABLE_GATE_ARCHITECTURE=true: architecture-reviewer runs despite docs-only" {
  cp "${FIXTURES_DIR}/docs_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/docs_only.files")
  AI_DISABLE_GATE_ARCHITECTURE=true detect_conditional_agent_triggers
  [ "$RUN_ARCHITECTURE_REVIEWER" = "true" ]
}

@test "AI_DISABLE_GATE_SECURITY=true: security-reviewer runs despite no security patterns" {
  cp "${FIXTURES_DIR}/docs_only.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/docs_only.files")
  AI_DISABLE_GATE_SECURITY=true detect_conditional_agent_triggers
  [ "$RUN_SECURITY_REVIEWER" = "true" ]
}

@test "AI_DISABLE_GATE_EDGE_CASE=true: edge-case-hunter runs despite no control flow" {
  cp "${FIXTURES_DIR}/no_control_flow.diff" "$DIFF_FILE"
  CHANGED_FILES=$(cat "${FIXTURES_DIR}/no_control_flow.files")
  AI_DISABLE_GATE_EDGE_CASE=true detect_conditional_agent_triggers
  [ "$RUN_EDGE_CASE_HUNTER" = "true" ]
}

# ---------------------------------------------------------------------------
# Defensive: empty inputs default all flags to safe/run state
# ---------------------------------------------------------------------------

@test "empty DIFF_FILE and CHANGED_FILES: arch and edge default to true; security skips (nothing to scan)" {
  : > "$DIFF_FILE"
  CHANGED_FILES=""
  detect_conditional_agent_triggers
  # Architecture gate needs CHANGED_FILES to evaluate; empty input → runs (safe default).
  [ "$RUN_ARCHITECTURE_REVIEWER" = "true" ]
  # Edge-case gate finds no control flow in empty diff → skips (correct, nothing to hunt).
  [ "$RUN_EDGE_CASE_HUNTER" = "false" ]
  # Security gate finds no keywords or sensitive paths in empty diff → skips (nothing to scan).
  [ "$RUN_SECURITY_REVIEWER" = "false" ]
  [ "$HAS_ERROR_PATTERNS" -eq 0 ]
}

# ---------------------------------------------------------------------------
# HAS_ERROR_PATTERNS (existing silent-failure-hunter trigger, now in function)
# ---------------------------------------------------------------------------

@test "HAS_ERROR_PATTERNS: set to 1 when diff contains 'catch'" {
  printf 'diff --git a/a.go b/a.go\n+  } catch (err) {\n' > "$DIFF_FILE"
  CHANGED_FILES="a.go"
  detect_conditional_agent_triggers
  [ "$HAS_ERROR_PATTERNS" -eq 1 ]
}

@test "HAS_ERROR_PATTERNS: 0 when diff has no error patterns" {
  cp "${FIXTURES_DIR}/no_control_flow.diff" "$DIFF_FILE"
  CHANGED_FILES="config/settings.go"
  detect_conditional_agent_triggers
  [ "$HAS_ERROR_PATTERNS" -eq 0 ]
}
