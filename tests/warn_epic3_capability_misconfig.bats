#!/usr/bin/env bats
# Tests for warn_epic3_capability_misconfig() in review.sh.
#
# The function surfaces a ::warning:: on stderr when an Epic 3 capability flag
# is truthy but the engine is not python. Issue surfaced by CodeRabbit on
# pulumi-lagoon-provider#212.

setup() {
  load test_helper

  load_function "${PROJECT_ROOT}/review.sh" warn_epic3_capability_misconfig

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

  run warn_epic3_capability_misconfig python
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "python engine: no flags set → no warning" {
  run warn_epic3_capability_misconfig python
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Bash engine: warn when capability flags are truthy
# ---------------------------------------------------------------------------

@test "bash engine: no flags → no warnings" {
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "bash engine: context-enrichment=true → exactly one warning naming Capability A" {
  export AI_CONTEXT_ENRICHMENT=true
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"AI_CONTEXT_ENRICHMENT=true"* ]]
  [[ "$output" == *"Capability A"* ]]
  # Should NOT mention the other two
  [[ "$output" != *"Capability B"* ]]
  [[ "$output" != *"Capability C"* ]]
}

@test "bash engine: feedback-loop=true → exactly one warning naming Capability C" {
  export AI_FEEDBACK_LOOP=true
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_FEEDBACK_LOOP=true"* ]]
  [[ "$output" == *"Capability C"* ]]
  [[ "$output" != *"Capability A"* ]]
  [[ "$output" != *"Capability B"* ]]
}

@test "bash engine: sarif-paths set → exactly one warning naming Capability B" {
  export AI_SARIF_PATHS=results/foo.sarif
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_SARIF_PATHS"* ]]
  [[ "$output" == *"Capability B"* ]]
  [[ "$output" != *"Capability A"* ]]
  [[ "$output" != *"Capability C"* ]]
}

@test "bash engine: all three flags set → three warnings, one per capability" {
  export AI_CONTEXT_ENRICHMENT=true
  export AI_FEEDBACK_LOOP=true
  export AI_SARIF_PATHS=results/codeql.sarif

  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"Capability A"* ]]
  [[ "$output" == *"Capability B"* ]]
  [[ "$output" == *"Capability C"* ]]
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
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "bash engine: feedback-loop empty string → no warning" {
  export AI_FEEDBACK_LOOP=""
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "bash engine: sarif-paths empty string → no warning" {
  export AI_SARIF_PATHS=""
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Truthy-value variants accepted by the bool parser
# ---------------------------------------------------------------------------

@test "bash engine: context-enrichment=True (capitalized) → warning fires" {
  export AI_CONTEXT_ENRICHMENT=True
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"Capability A"* ]]
}

@test "bash engine: feedback-loop=1 (numeric) → warning fires" {
  export AI_FEEDBACK_LOOP=1
  run warn_epic3_capability_misconfig bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"Capability C"* ]]
}

# ---------------------------------------------------------------------------
# Other engine values: treated like bash (warn on truthy flags)
# ---------------------------------------------------------------------------

@test "unknown engine: context-enrichment=true → warning fires" {
  export AI_CONTEXT_ENRICHMENT=true
  run warn_epic3_capability_misconfig some-future-engine
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=some-future-engine"* ]]
  [[ "$output" == *"Capability A"* ]]
}
