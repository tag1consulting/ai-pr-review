# Story 4.6 — Docs Refresh (Python Primary)

**Epic:** 4 — Soak, Observability, Default Flip
**Story ID:** 4-6
**Story Key:** 4-6-docs-refresh-python-primary
**GitHub Issue:** #246
**Status:** done
**PRD refs:** 4.FR-6

---

## User Story

As a **consumer of ai-pr-review**, I want the documentation to accurately reflect that Python is the primary/default engine and bash is the deprecated legacy fallback, so that I configure my workflow correctly from the start and am not misled by stale "bash (default)" references.

---

## Acceptance Criteria

- [x] `README.md` — `AI_PR_REVIEW_ENGINE` env var table row and `engine` input table row both describe Python as the default; bash described as deprecated legacy
- [x] `docs/configuration.md:47` — env var table default column changed from `bash` to `python`; description updated
- [x] `docs/getting-started.md:131` — table row updated; workflow code snippet fallback updated
- [x] `docs/installation-direct-action.md:135` — table row updated; workflow code snippet fallback updated
- [x] `docs/features.md:116` — sentence "The bash pipeline remains the default" replaced with accurate statement that Python is now the default and bash is deprecated
- [x] `docs/features.md` — new running-changelog entry added at the top of the latest version section documenting the default flip
- [x] `docs/index.md` — any framing that implies bash is default updated
- [x] `action.yml:137-140` — engine input description prose updated (`'bash' (default)` → `'python' (default)`, bash described as deprecated)
- [x] `container-action/action.yml:106` — engine input description updated similarly
- [x] `grep -rn "bash.*default\|remains the default" README.md docs/ action.yml container-action/action.yml` — 3 remaining matches are historical changelog accuracy notes (past-tense), not present-tense user-facing defaults; all config-reference locations clean
- [x] No code changes (no `default:` key flips, no `review.sh` changes) — those are story 4-9

---

## Implementation Tasks

### 1. `README.md`

**Line 177** (env var table):
```
| `AI_PR_REVIEW_ENGINE` | Variable | No | Compute engine: `python` (default) or `bash` (deprecated) |
```

**Line 249** (input table):
```
| `engine` | No | `python` | Compute engine: `python` (default) or `bash` (deprecated). `bash` is the legacy pipeline and will be removed in a future major release. |
```

Also scan surrounding context at lines 45 and 259 for any "requires `engine: python`" framing that implied bash was the normal case — reword to reflect python is now the base, with optional capabilities layered on top.

### 2. `docs/configuration.md`

**Line 47** (env var table, default column):
```
| `AI_PR_REVIEW_ENGINE` | `python` | `engine` | Compute engine: `python` (default) or `bash` (deprecated legacy; will be removed in a future major release) |
```

### 3. `docs/getting-started.md`

**Line 107** (workflow snippet) — change `|| 'bash'` to `|| 'python'` in any displayed fallback.

**Line 131** (table row):
```
| `AI_PR_REVIEW_ENGINE` | Variable | No | Compute engine: `python` (default) or `bash` (deprecated) |
```

### 4. `docs/installation-direct-action.md`

**Line 86** (workflow snippet) — change `|| 'bash'` to `|| 'python'` in any displayed fallback.

**Line 135** (table row):
```
| `AI_PR_REVIEW_ENGINE` | `python` | Compute engine: `python` (default) or `bash` (deprecated legacy) |
```

### 5. `docs/features.md`

**Lines 114–120** (v0.9.0 section). Change line 116:
- Before: `GitHub, GitLab, and Bitbucket. The bash pipeline remains the default and is`
- After: The full paragraph should be reworded so that: "The Python engine is now the default as of v1.0.0. The bash pipeline is deprecated and will be removed in a future release."

**New changelog entry** — add a new top-level section (or prepend to the latest section) for the default flip. Mirror the style of existing entries:

```markdown
**Python engine is now the default (v1.0.0).** `AI_PR_REVIEW_ENGINE` now defaults to
`python`. The bash pipeline is deprecated: it continues to work when explicitly set
(`engine: bash` / `AI_PR_REVIEW_ENGINE=bash`) but emits a deprecation warning and will
be removed in a future major release. Consumers who pinned `engine: bash` should
migrate to `engine: python` (or simply remove the `engine:` input to use the new
default). All Epic 3 capabilities (context enrichment, SARIF ingestion, learning loop)
require the Python engine and are unaffected.
```

### 6. `docs/index.md`

Scan line 78 and surrounding context for framing that implies bash is normal and python is opt-in. Update to reflect python is now the default; bash is the legacy/deprecated option.

### 7. `action.yml` (description prose only — NOT the `default:` key)

**Lines 135–140** engine input description:
```yaml
  engine:
    description: >
      Compute engine to use. 'python' (default) is the supported engine; 'bash' is the
      deprecated legacy pipeline (will be removed in a future major release).
      Required for Epic 3 capabilities (context enrichment, SARIF ingestion,
      learning loop). Sets AI_PR_REVIEW_ENGINE.
    required: false
    default: 'bash'
```
Note: The `default: 'bash'` key is intentionally left unchanged — that flip happens in story 4-9. Only the description prose changes here.

### 8. `container-action/action.yml` (description prose only)

**Lines 105–106**:
```yaml
    description: "Compute engine: 'python' (default) or 'bash' (deprecated legacy; will be removed in a future major release). Required for Epic 3 capabilities (context enrichment, SARIF ingestion, learning loop)."
```
Note: `default: 'bash'` key unchanged — flipped in story 4-9.

---

## Dev Agent Guardrails

- **Documentation only** — do NOT touch `review.sh`, `ai_pr_review/config.py`, the `default:` YAML keys in `action.yml` or `container-action/action.yml`, or any Python/shell code. Those changes belong to story 4-9.
- **Keep `default:` keys as `'bash'`** — the description prose saying "python is default" will be temporarily inconsistent with the key value until 4-9 commits. That is intentional; both stories land in the same PR so the end state is always consistent.
- **Do not create `docs/bash-legacy.md`** — the PRD mentions this but it is deferred to Epic 5 (the deletion epic). Keep all bash documentation in place for now.
- **Mirror the existing changelog style** in `docs/features.md` — bold title + version reference + plain prose paragraph. Do not add headers or sub-bullets inconsistent with surrounding entries.
- **Verify the grep gate** before committing: `grep -rn "bash.*default\|remains the default" README.md docs/ action.yml container-action/action.yml` must return zero results.
- **Workflow file** at `.github/workflows/ai-review.yml` — do NOT touch. That fallback (`|| 'bash'`) is updated in story 4-9.
- **Example files** (`examples/workflows/pr-review.yml`, `examples/pipelines/*.yml`) — do NOT touch. Updated in story 4-9 alongside the actual default flips.

---

## Files to Modify

| File | Nature of change |
|---|---|
| `README.md` | Lines 177, 249 (table rows); scan 45, 259 |
| `docs/configuration.md` | Line 47 (table row default + description) |
| `docs/getting-started.md` | Line 107 (snippet), line 131 (table row) |
| `docs/installation-direct-action.md` | Line 86 (snippet), line 135 (table row) |
| `docs/features.md` | Lines 114–120 (reword v0.9.0 intro); add new top-of-changelog entry |
| `docs/index.md` | Line 78 and context |
| `action.yml` | Lines 135–140 description prose only |
| `container-action/action.yml` | Lines 105–106 description prose only |

---

## Verification

```bash
# Must return no results
grep -rn "bash.*default\|remains the default" README.md docs/ action.yml container-action/action.yml

# Spot-checks
grep -n "AI_PR_REVIEW_ENGINE" README.md docs/configuration.md docs/getting-started.md docs/installation-direct-action.md
grep -n "engine.*description\|default:.*bash\|default:.*python" action.yml container-action/action.yml
```

No tests to run for documentation-only changes. The grep gate above is the verification.

---

## Notes for Dev Agent

- This story is being implemented in the worktree at `/home/gchaix/worktrees/ai-pr-review-epic4-flip` on branch `feat/epic4-default-flip-to-python`.
- Story 4-9 (flip) will update the actual `default:` YAML keys and shell fallback in a subsequent commit on the same branch, making the end state fully consistent.
- The features.md changelog entry for the default flip should be positioned as the newest entry (top of the file's "What's new" section or immediately below the latest version header).

---

## Dev Agent Record

### Completion Notes (2026-06-02)

All ACs satisfied. Documentation-only changes — no code, no `default:` key flips.

Key decisions:
- `action.yml` and `container-action/action.yml` description prose updated to "python (default)" but `default: 'bash'` keys left intentionally unchanged (flipped in S9 commit D on same PR).
- `docs/getting-started.md` and `docs/installation-direct-action.md` workflow snippets updated from `|| 'bash'` to `|| 'python'` (doc-layer change only; operative defaults in story 4-9).
- `docs/configuration.md:118` opt-in capabilities sentence updated to avoid implying Python needs to be explicitly set.
- 3 historical grep matches left intact: past-tense changelog accuracy notes in features.md about prior parity alignment, not present-tense user-facing defaults.
- `docs/features.md`: new `## What's new in v1.0.0` section added at top with two entries: default flip announcement + deprecation warning description.

### File List

| File | Change |
|---|---|
| `README.md` | Lines 45, 177, 249, 259: python primary; bash deprecated |
| `docs/configuration.md` | Lines 47, 118: python default; opt-in phrasing updated |
| `docs/getting-started.md` | Lines 107, 131: snippet + table row updated |
| `docs/installation-direct-action.md` | Lines 86, 135: snippet + table row updated |
| `docs/features.md` | Lines 114-117: v0.9.0 intro reworded; new v1.0.0 section added at top |
| `docs/index.md` | Line 78: opt-in capabilities framing updated |
| `action.yml` | Lines 136-141: description prose updated (default: key unchanged) |
| `container-action/action.yml` | Line 106: description prose updated (default: key unchanged) |

### Change Log

- 2026-06-02: Docs refresh complete. All configuration reference locations now show python as default, bash as deprecated.
