# Story 4.9 — Flip Default Engine to Python

**Epic:** 4 — Soak, Observability, Default Flip
**Story ID:** 4-9
**Story Key:** 4-9-flip-default-engine
**GitHub Issue:** #249
**Status:** done
**PRD refs:** 4.FR-9
**Gated on:** Story 4-7 (field soak — EXIT CRITERIA NOW MET as of 2026-06-02)
**Blocks:** Story 4-10 (release)

---

## User Story

As a **consumer of ai-pr-review**, I want the Python engine to be the default when I don't specify `engine:` in my workflow, so that I automatically get the supported, feature-complete engine without any configuration change.

---

## Soak Gate — CLEARED

All three soak exit criteria are met (verified 2026-06-02):

| Criterion | Required | Actual |
|---|---|---|
| Calendar days since 2026-05-18 | ≥14 | 15 ✅ |
| Real engine=python reviews | ≥30 | 80 ✅ |
| Zero open P0/P1 python-engine bugs (consecutive days) | ≥7 | 14 ✅ |

The `soak-log.md` "Flip authorized: NO" line is stale — flip is authorized.

---

## Acceptance Criteria

- [x] `review.sh` — `AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-bash}"` changed to `:-python`; inline comment updated
- [x] `ai_pr_review/config.py` — field default `"bash"` → `"python"` and `from_env()` env fallback `"bash"` → `"python"`
- [x] `action.yml` — `default: 'bash'` changed to `default: 'python'`
- [x] `container-action/action.yml` — `default: 'bash'` changed to `default: 'python'`
- [x] `.github/workflows/ai-review.yml` — `|| 'bash'` changed to `|| 'python'`
- [x] `examples/workflows/pr-review.yml` — `|| 'bash'` changed to `|| 'python'`; comment updated
- [x] `examples/README.md` — snippet `|| 'bash'` changed to `|| 'python'`
- [x] `examples/pipelines/.gitlab-ci.yml` — comment + `:-bash` changed to `:-python`
- [x] `examples/pipelines/bitbucket-pipelines.yml` — comment + `:-bash` changed to `:-python`
- [x] `tests/python/test_config.py:33` — `assert cfg.engine == "bash"` updated to `"python"`
- [x] `bats tests/*.bats` passes — 685 tests, 0 failures
- [x] `pytest tests/python/` passes — 972 tests, 0 failures
- [x] `shellcheck review.sh` passes — exit 0
- [x] End-to-end smoke: `python -c "from ai_pr_review.config import ReviewConfig; c = ReviewConfig.from_env(); assert c.engine == 'python'"` → ✅
- [x] End-to-end smoke: shell `AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-python}"` → `python` ✅

---

## Implementation Tasks

### 1. `review.sh` — runtime default

**Line 345:**
```bash
# Before:
AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-bash}"
# After:
AI_PR_REVIEW_ENGINE="${AI_PR_REVIEW_ENGINE:-python}"
```

### 2. `ai_pr_review/config.py` — Python CLI default

**Line ~352** (in `from_env()` or `ReviewConfig` construction):
```python
# Before:
engine=os.environ.get("AI_PR_REVIEW_ENGINE", "bash"),
# After:
engine=os.environ.get("AI_PR_REVIEW_ENGINE", "python"),
```

### 3. `action.yml` — composite action input default

**Line 142:**
```yaml
# Before:
    default: 'bash'
# After:
    default: 'python'
```
(The engine input description prose was already updated in story 4-6.)

### 4. `container-action/action.yml` — container action input default

**Line 108:**
```yaml
# Before:
    default: 'bash'
# After:
    default: 'python'
```

### 5. `.github/workflows/ai-review.yml` — this repo's own workflow

**Line ~265:**
```yaml
# Before:
engine: ${{ vars.AI_PR_REVIEW_ENGINE || 'bash' }}
# After:
engine: ${{ vars.AI_PR_REVIEW_ENGINE || 'python' }}
```
Note: the repo variable `AI_PR_REVIEW_ENGINE` is already set to `python`, so behavior is unchanged; this is consistency/defense-in-depth.

### 6. `examples/workflows/pr-review.yml` — GitHub Actions example

Find and change `|| 'bash'` fallback(s) to `|| 'python'`. Also update any comment that says `(default: bash)` to `(default: python)`.

### 7. `examples/pipelines/.gitlab-ci.yml` — GitLab example

Find and change `${AI_PR_REVIEW_ENGINE:-bash}` to `${AI_PR_REVIEW_ENGINE:-python}` (two occurrences: ~lines 17 and 63). Update any comment `# default: bash` to `# default: python`.

### 8. `examples/pipelines/bitbucket-pipelines.yml` — Bitbucket example

Find and change `${AI_PR_REVIEW_ENGINE:-bash}` to `${AI_PR_REVIEW_ENGINE:-python}`. Update any comment.

### 9. Python test updates

Search `tests/python/` for any assertion of the form `engine == "bash"` or `engine="bash"` used as the default. Update to `"python"`. Most likely location: `tests/python/test_config.py`. Check with:
```bash
grep -rn '"bash"\|bash.*engine\|engine.*bash' tests/python/ | grep -v "# "
```

---

## Dev Agent Guardrails

- **This commit is isolated and revertable** — `git revert <sha>` of this commit alone backs out the default flip without touching docs (4-6) or the warning function (4-8). Keep all five default flip changes in this single commit to preserve that property.
- **Do NOT modify story files, soak-log, or sprint-status** in this commit — those were handled in the bookkeeping commit.
- **Do NOT modify `review.sh` warning function or placement** — that belongs to story 4-8.
- **Verify `config.py` line number** before editing — the `from_env()` method may have shifted since the plan was written. Grep: `grep -n '"bash"' ai_pr_review/config.py`
- **Verify `action.yml` line number**: `grep -n "default:.*bash" action.yml container-action/action.yml`
- **Run the full test suite** before committing: `bats tests/*.bats && pytest tests/python/`

---

## Files to Modify

| File | Change |
|---|---|
| `review.sh` | Line ~345: `:-bash` → `:-python` |
| `ai_pr_review/config.py` | Line ~352: `"bash"` default → `"python"` |
| `action.yml` | Line ~142: `default: 'bash'` → `default: 'python'` |
| `container-action/action.yml` | Line ~108: `default: 'bash'` → `default: 'python'` |
| `.github/workflows/ai-review.yml` | Line ~265: `|| 'bash'` → `|| 'python'` |
| `examples/workflows/pr-review.yml` | `|| 'bash'` → `|| 'python'`; comment update |
| `examples/pipelines/.gitlab-ci.yml` | Both `:-bash` → `:-python`; comment update |
| `examples/pipelines/bitbucket-pipelines.yml` | `:-bash` → `:-python`; comment update |
| `tests/python/test_config.py` (likely) | Any `"bash"` default assertion → `"python"` |

---

## Verification

```bash
# From the worktree at /home/gchaix/worktrees/ai-pr-review-epic4-flip:

# Full test suites
bats tests/*.bats
pytest tests/python/
shellcheck review.sh lib/*.sh
actionlint .github/workflows/*.yml

# Grep gate — must return zero "bash default" references
grep -rn ':-bash\|"bash"\|default.*bash\|bash.*default' \
  review.sh ai_pr_review/config.py action.yml container-action/action.yml \
  .github/workflows/ai-review.yml \
  examples/workflows/pr-review.yml \
  examples/pipelines/ | grep -v "# " | grep -v "warn_bash"

# End-to-end default proof (dry run, no real API calls):
# 1. Python is the new default (no deprecation warning):
unset AI_PR_REVIEW_ENGINE
AI_DRY_RUN=1 AI_PR_NUMBER=1 bash review.sh 2>&1 | grep -E "Engine:|::warning::" | head

# 2. Explicit bash triggers deprecation warning:
AI_PR_REVIEW_ENGINE=bash AI_DRY_RUN=1 AI_PR_NUMBER=1 bash review.sh 2>&1 | grep "::warning::" | head

# 3. Python config default:
python3 -c "
import os
os.environ.pop('AI_PR_REVIEW_ENGINE', None)
from ai_pr_review.config import ReviewConfig
c = ReviewConfig.from_env()
assert c.engine == 'python', f'Expected python, got {c.engine}'
print('Config default: OK')
"
```

---

## Notes for Dev Agent

- This is story 4-9 — the flip. It MUST be committed after story 4-8 (which adds `warn_bash_engine_deprecated()`) because this commit makes the fall-through branch the explicit-bash-only path. The function needs to exist first.
- This commit's SHA should be noted in the PR description so reviewers can review the flip in isolation if desired.
- The `soak-log.md` "Flip authorized: YES" update was done in the bookkeeping commit (Commit A) — do not re-edit it here.

---

## Dev Agent Record

### Completion Notes (2026-06-02)

All ACs satisfied. This commit is isolated and independently revertable — reverting it alone backs out the default flip without touching docs (S6) or the warning function (S8).

Key changes:
- `review.sh`: `:-bash` → `:-python`; inline comment updated to reflect v1.0.0
- `ai_pr_review/config.py`: both field default and `from_env()` env fallback changed from `"bash"` to `"python"`
- `action.yml` + `container-action/action.yml`: `default: 'bash'` → `default: 'python'`
- `.github/workflows/ai-review.yml`: consistency update (repo var already python)
- `examples/`: all `:-bash` / `|| 'bash'` fallbacks updated; comments updated
- `tests/python/test_config.py:33`: `"bash"` → `"python"` for default assertion

Verified: 972 Python tests, 685 bats tests — all pass. shellcheck clean. Python config default confirmed via smoke test.

### File List

| File | Change |
|---|---|
| `review.sh` | Line ~359: `:-bash` → `:-python`; comment updated |
| `ai_pr_review/config.py` | Line ~190: field default; line ~352: `from_env()` fallback |
| `action.yml` | Line 142: `default: 'bash'` → `default: 'python'` |
| `container-action/action.yml` | Line 108: `default: 'bash'` → `default: 'python'` |
| `.github/workflows/ai-review.yml` | Line 265: `|| 'bash'` → `|| 'python'` |
| `examples/workflows/pr-review.yml` | Line 84: fallback + comment updated |
| `examples/README.md` | Line 23: fallback updated |
| `examples/pipelines/.gitlab-ci.yml` | Lines 17-18, 63: comment + `:-bash` → `:-python` |
| `examples/pipelines/bitbucket-pipelines.yml` | Lines 23-24, 69: comment + `:-bash` → `:-python` |
| `tests/python/test_config.py` | Line 33: `"bash"` → `"python"` default assertion |

### Change Log

- 2026-06-02: Default engine flipped to python as of v1.0.0. All 9 touch-points updated. Tests passing.
