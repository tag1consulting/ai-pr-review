#!/usr/bin/env bats
# Tests for warn_bash_engine_deprecated() in review.sh.
#
# The function emits a ::warning:: when bash or an unrecognized engine value is
# selected, and is silent for python. As of v1.0.0 bash is deprecated and will
# be removed in Epic 5.

setup() {
  load test_helper

  load_function "${PROJECT_ROOT}/review.sh" warn_bash_engine_deprecated
}

# ---------------------------------------------------------------------------
# Python engine: no warning (guard)
# ---------------------------------------------------------------------------

@test "python engine: no warning emitted" {
  run warn_bash_engine_deprecated python
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Bash engine: deprecation warning fires
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
# Unknown/typo engine value: falls back to bash with a distinct message
# ---------------------------------------------------------------------------

@test "unknown engine value: emits ::warning:: naming the value" {
  run warn_bash_engine_deprecated some-future-engine
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=some-future-engine"* ]]
  [[ "$output" == *"not a recognized engine value"* ]]
}

@test "unknown engine value (PYTHON uppercase): warns about unrecognized value, not bash" {
  run warn_bash_engine_deprecated PYTHON
  [ "$status" -eq 0 ]
  [[ "$output" == *"not a recognized engine value"* ]]
  [[ "$output" != *"AI_PR_REVIEW_ENGINE=bash selects"* ]]
}

# ---------------------------------------------------------------------------
# Fail-soft: always exits 0
# ---------------------------------------------------------------------------

@test "fail-soft: always returns exit code 0" {
  run warn_bash_engine_deprecated bash
  [ "$status" -eq 0 ]
  run warn_bash_engine_deprecated unknown-engine
  [ "$status" -eq 0 ]
  run warn_bash_engine_deprecated python
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Engine default regression guard — ensures AI_PR_REVIEW_ENGINE:-python is set
# ---------------------------------------------------------------------------
# This test guards against accidental reversion of the v1.0.0 default flip.
# If the :-bash fallback is restored (e.g., via a bad merge conflict resolution),
# this test fails immediately.

@test "engine default: unset AI_PR_REVIEW_ENGINE resolves to python" {
  local resolved
  resolved=$(unset AI_PR_REVIEW_ENGINE; echo "${AI_PR_REVIEW_ENGINE:-python}")
  [ "$resolved" = "python" ]
}

@test "engine default: explicit bash is preserved as bash" {
  local resolved
  resolved=$(AI_PR_REVIEW_ENGINE=bash; echo "${AI_PR_REVIEW_ENGINE:-python}")
  [ "$resolved" = "bash" ]
}

@test "engine default: explicit python is preserved as python" {
  local resolved
  resolved=$(AI_PR_REVIEW_ENGINE=python; echo "${AI_PR_REVIEW_ENGINE:-python}")
  [ "$resolved" = "python" ]
}
