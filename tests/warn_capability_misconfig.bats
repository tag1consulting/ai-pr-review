#!/usr/bin/env bats
# Tests for warn_capability_misconfig() in review.sh.
#
# The function surfaces a ::warning:: on stderr when a Python-only capability
# flag is truthy but the engine is not python.

setup() {
  load test_helper

  load_function "${PROJECT_ROOT}/review.sh" warn_capability_misconfig

  # Clear all relevant env vars so each test starts from a known state.
  unset AI_CONTEXT_ENRICHMENT AI_FEEDBACK_LOOP AI_SARIF_PATHS
}

# ---------------------------------------------------------------------------
# Python engine: no warnings ever (flags are honored, not ignored)
# ---------------------------------------------------------------------------

@test "python engine: all flags set → no warning" {
  export AI_CONTEXT_ENRICHMENT=true
  export AI_FEEDBACK_LOOP=true
  export AI_SARIF_PATHS=results/codeql.sarif

  run warn_capability_misconfig python
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "python engine: no flags set → no warning" {
  run warn_capability_misconfig python
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Bash engine: warn when capability flags are truthy
# ---------------------------------------------------------------------------

@test "bash engine: no flags → no warnings" {
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "bash engine: context-enrichment=true → exactly one warning naming AI_CONTEXT_ENRICHMENT" {
  export AI_CONTEXT_ENRICHMENT=true
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"AI_CONTEXT_ENRICHMENT=true"* ]]
  # Should NOT mention the other two
  [[ "$output" != *"AI_FEEDBACK_LOOP"* ]]
  [[ "$output" != *"AI_SARIF_PATHS"* ]]
}

@test "bash engine: feedback-loop=true → exactly one warning naming AI_FEEDBACK_LOOP" {
  export AI_FEEDBACK_LOOP=true
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_FEEDBACK_LOOP=true"* ]]
  [[ "$output" != *"AI_CONTEXT_ENRICHMENT"* ]]
  [[ "$output" != *"AI_SARIF_PATHS"* ]]
}

@test "bash engine: sarif-paths set → exactly one warning naming AI_SARIF_PATHS" {
  export AI_SARIF_PATHS=results/foo.sarif
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_SARIF_PATHS"* ]]
  [[ "$output" != *"AI_CONTEXT_ENRICHMENT"* ]]
  [[ "$output" != *"AI_FEEDBACK_LOOP"* ]]
}

@test "bash engine: all three flags set → three warnings" {
  export AI_CONTEXT_ENRICHMENT=true
  export AI_FEEDBACK_LOOP=true
  export AI_SARIF_PATHS=results/codeql.sarif

  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_CONTEXT_ENRICHMENT"* ]]
  [[ "$output" == *"AI_SARIF_PATHS"* ]]
  [[ "$output" == *"AI_FEEDBACK_LOOP"* ]]
  # Three independent ::warning:: lines
  local count
  count=$(printf '%s\n' "$output" | grep -c "^::warning::")
  [ "$count" -eq 3 ]
}

# ---------------------------------------------------------------------------
# Bash engine: falsy flag values → no warnings
# ---------------------------------------------------------------------------

@test "bash engine: context-enrichment=false → no warning" {
  export AI_CONTEXT_ENRICHMENT=false
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "bash engine: feedback-loop empty string → no warning" {
  export AI_FEEDBACK_LOOP=""
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "bash engine: sarif-paths empty string → no warning" {
  export AI_SARIF_PATHS=""
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Truthy-value variants accepted by the bool parser
# ---------------------------------------------------------------------------

@test "bash engine: context-enrichment=True (capitalized) → warning fires" {
  export AI_CONTEXT_ENRICHMENT=True
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_CONTEXT_ENRICHMENT"* ]]
}

@test "bash engine: feedback-loop=1 (numeric) → warning fires" {
  export AI_FEEDBACK_LOOP=1
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_FEEDBACK_LOOP"* ]]
}

@test "bash engine: context-enrichment=yes → warning fires (parity with Python _bool)" {
  # Python ai_pr_review/config.py:_bool() accepts true/1/yes case-insensitively.
  # Without lowercase normalization the bash guard would miss this form and
  # the operator's capability flag would be silently dropped on the bash engine.
  export AI_CONTEXT_ENRICHMENT=yes
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_CONTEXT_ENRICHMENT=yes"* ]]
}

@test "bash engine: feedback-loop=YES (uppercase) → warning fires" {
  export AI_FEEDBACK_LOOP=YES
  run warn_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_FEEDBACK_LOOP"* ]]
}

# ---------------------------------------------------------------------------
# Other engine values: treated like bash (warn on truthy flags)
# ---------------------------------------------------------------------------

@test "unknown engine: context-enrichment=true → warning fires" {
  export AI_CONTEXT_ENRICHMENT=true
  run warn_capability_misconfig some-future-engine
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=some-future-engine"* ]]
  [[ "$output" == *"AI_CONTEXT_ENRICHMENT"* ]]
}
