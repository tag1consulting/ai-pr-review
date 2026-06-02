# Story 4.8 — Deprecation Warning on AI_PR_REVIEW_ENGINE=bash

**Epic:** 4 — Soak, Observability, Default Flip
**Story ID:** 4-8
**Story Key:** 4-8-deprecation-warning-bash
**GitHub Issue:** #248
**Status:** ready-for-dev
**PRD refs:** 4.FR-8

---

## User Story

As a **consumer who has explicitly set `AI_PR_REVIEW_ENGINE=bash`**, I want to see a clear deprecation warning in the workflow logs, so that I know the bash engine is deprecated and can migrate to the Python engine before it is removed in a future major release.

---

## Acceptance Criteria

- [ ] A new function `warn_bash_engine_deprecated()` is added to `review.sh`, defined alongside `warn_epic3_capability_misconfig()` (after ~line 227)
- [ ] The function emits a single `::warning::` annotation line to stderr (GitHub Actions format, also visible on GitLab/Bitbucket as plain stderr)
- [ ] The function is called in `main()` in the fall-through (non-python) branch — after the `if [[ "$AI_PR_REVIEW_ENGINE" == "python" ]] … fi` block and before Phase 0
- [ ] The function does NOT fire when `AI_PR_REVIEW_ENGINE=python` (or when engine is unset/defaults to python after story 4-9)
- [ ] The warning is fail-soft: always returns exit code 0 and never blocks the review
- [ ] `tests/warn_bash_engine_deprecated.bats` is created with tests covering: bash engine → warning fires; unknown engine value → named in warning; always returns 0
- [ ] All existing `bats tests/*.bats` continue to pass

---

## Implementation Tasks

### 1. Add `warn_bash_engine_deprecated()` to `review.sh`

Add after `warn_epic3_capability_misconfig()` (currently ending at ~line 227). Follow the identical pattern — top-level function, `echo "::warning::..." >&2`, no `exit`, no `set -e` interaction.

**Function signature and body:**

```bash
# Emit a deprecation warning when the legacy bash engine is explicitly selected.
# As of v1.0.0 the default engine is "python"; this path is reached only when
# AI_PR_REVIEW_ENGINE=bash (or an unknown value) is set explicitly. The bash
# pipeline is scheduled for removal in Epic 5.
# Fail-soft: always returns 0 and never blocks the review.
# Arguments: $1 — the resolved AI_PR_REVIEW_ENGINE value
warn_bash_engine_deprecated() {
  local engine="$1"
  echo "::warning::AI_PR_REVIEW_ENGINE=${engine} selects the legacy bash engine, which is DEPRECATED as of v1.0.0. The Python engine is now the default and the bash pipeline will be removed in a future major release. Set AI_PR_REVIEW_ENGINE=python (or unset it) to use the supported engine." >&2
}
```

### 2. Call site in `main()`

In `review.sh main()`, the dispatch block looks like (around lines 345–365):

```bash
AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-bash}"   # line 345 (becomes :-python in story 4-9)
...
warn_epic3_capability_misconfig "$AI_PR_REVIEW_ENGINE"  # line 350
...
if [[ "$AI_PR_REVIEW_ENGINE" == "python" ]]; then
  ...python dispatch...
  exit 0
fi
```

Add the call **after** the `fi` that closes the python block, **before** Phase 0:

```bash
fi   # end python early-exit block

warn_bash_engine_deprecated "$AI_PR_REVIEW_ENGINE"

# ---- Phase 0: Static analyzers ---
```

This guarantees the warning fires only when the python path was NOT taken (i.e., bash or unknown engine value).

### 3. Add `tests/warn_bash_engine_deprecated.bats`

Mirror the structure of `tests/warn_epic3_capability_misconfig.bats` (load_function pattern):

```bash
#!/usr/bin/env bats
# Tests for warn_bash_engine_deprecated() in review.sh

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/review.sh" warn_bash_engine_deprecated
}

@test "bash engine: emits ::warning:: mentioning DEPRECATED and python" {
  run warn_bash_engine_deprecated bash
  [ "$status" -eq 0 ]
  [[ "$output" == *"::warning::"* ]]
  [[ "$output" == *"DEPRECATED"* ]]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=bash"* ]]
  [[ "$output" == *"python"* ]]
}

@test "unknown engine value: warning names the value" {
  run warn_bash_engine_deprecated some-future-engine
  [ "$status" -eq 0 ]
  [[ "$output" == *"AI_PR_REVIEW_ENGINE=some-future-engine"* ]]
  [[ "$output" == *"DEPRECATED"* ]]
}

@test "fail-soft: always returns exit code 0" {
  run warn_bash_engine_deprecated bash
  [ "$status" -eq 0 ]
}
```

Adapt the exact `load_function` helper call to match how `warn_epic3_capability_misconfig.bats` does it — do not invent a new helper pattern.

---

## Dev Agent Guardrails

- **Do not touch the engine dispatch logic** (`AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-...}"` and the `if [[ == "python" ]]` block) — those changes belong to story 4-9.
- **Follow `warn_epic3_capability_misconfig()` exactly**: same `echo "::warning::..." >&2` pattern, same fail-soft (no `exit`), same top-level function placement in `review.sh`.
- **Check `tests/warn_epic3_capability_misconfig.bats`** before writing the new test file — match its exact `setup()`, `load_function`, and assertion style.
- **`shellcheck review.sh`** must pass after the change. Quote the `$engine` argument correctly in the function body.
- **The warning text must include** `::warning::`, `DEPRECATED`, the engine value (`AI_PR_REVIEW_ENGINE=${engine}`), and `python` (to tell the user what to use instead).

---

## Files to Modify / Create

| File | Change |
|---|---|
| `review.sh` | Add `warn_bash_engine_deprecated()` function; add call in `main()` |
| `tests/warn_bash_engine_deprecated.bats` | Create — 3+ test cases |

---

## Existing Patterns to Reuse

- **Function template**: `review.sh` `warn_epic3_capability_misconfig()` (lines ~201–227)
- **Test template**: `tests/warn_epic3_capability_misconfig.bats`
- **`::warning::` format**: already used at review.sh:221, 225 — same stderr annotation syntax

---

## Verification

```bash
# From the worktree at /home/gchaix/worktrees/ai-pr-review-epic4-flip:
bats tests/warn_bash_engine_deprecated.bats
bats tests/warn_epic3_capability_misconfig.bats   # regression check
shellcheck review.sh

# End-to-end smoke (AI_DRY_RUN=1 to avoid real API calls):
# After story 4-9 flips the default: AI_PR_REVIEW_ENGINE=bash should trigger warning
# After story 4-9: unset AI_PR_REVIEW_ENGINE should NOT trigger warning (python path taken)
```

---

## Notes for Dev Agent

- This story is implemented in the worktree at `/home/gchaix/worktrees/ai-pr-review-epic4-flip` on branch `feat/epic4-default-flip-to-python`.
- Story 4-9 (flip) flips the `:-bash` fallback to `:-python`. Story 4-8 (this story) adds the warning in the fall-through branch. Since both land in the same PR, the net behavior after both commits is: python by default (no warning), bash on explicit request (warning fires).
- Story 4-9 must be committed AFTER this story so the warning function exists when 4-9 makes the fall-through branch the explicit-only path.
