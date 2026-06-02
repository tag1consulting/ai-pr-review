#!/usr/bin/env bats
# Tests for warn_bash_engine_deprecated() in review.sh.
#
# The function emits a ::warning:: on stderr when the bash engine is explicitly
# selected, signalling that it is deprecated as of v1.0.0 and will be removed
# in a future major release (Epic 5).

setup() {
  load test_helper

  load_function "${PROJECT_ROOT}/review.sh" warn_bash_engine_deprecated
}

# ---------------------------------------------------------------------------
# Bash engine: warning fires
# ---------------------------------------------------------------------------

@test "bash engine: emits ::warning:: containing DEPRECATED and python" {
  run warn_bash_engine_deprecated bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"DEPRECATED"* ]]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=bash"* ]]
  [[ "$output" == *"python"* ]]
}

@test "bash engine: warning mentions removal in future major release" {
  run warn_bash_engine_deprecated bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"future major release"* ]]
}

# ---------------------------------------------------------------------------
# Unknown engine value: warning fires and names the value
# ---------------------------------------------------------------------------

@test "unknown engine value: warning names the value passed in" {
  run warn_bash_engine_deprecated some-future-engine
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=some-future-engine"* ]]
  [[ "$output" == *"DEPRECATED"* ]]
}

# ---------------------------------------------------------------------------
# Fail-soft: always exits 0
# ---------------------------------------------------------------------------

@test "fail-soft: always returns exit code 0 for bash" {
  run warn_bash_engine_deprecated bash
  [ "$status" -eq 0 ]
}

@test "fail-soft: always returns exit code 0 for unknown value" {
  run warn_bash_engine_deprecated unknown-engine
  [ "$status" -eq 0 ]
}
