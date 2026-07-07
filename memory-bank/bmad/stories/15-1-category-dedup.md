# Story 15.1: Wire Category Field into Merge/Dedup Logic

**Epic:** 15 — Category-field follow-ups
**Story ID:** 15-1
**Story Key:** 15-1-category-dedup
**GitHub Issue:** [#578](https://github.com/tag1consulting/ai-pr-review/issues/578)
**Status:** done

---

## Story

As a **maintainer**,
I want `ai_pr_review/findings/merge.py`'s proximity-based dedup to treat `category` as a clustering signal,
so that two genuinely different findings on the same/nearby line (e.g. a secret exposure and an unrelated injection flaw) don't silently collapse into one representative finding.

---

## Acceptance Criteria

1. `_dedup_file()` only clusters two findings within `PROXIMITY_LINES` when their categories are compatible: identical, or at least one is `"other"` (the wildcard case — `"other"` never blocks a merge, since it means "no real category signal," not "genuinely different").
2. `_collapse_cluster()` preserves a real (non-`"other"`) category on the merged finding even when the highest-severity cluster member happens to be tagged `"other"`.
3. Existing corroboration tests (`test_merge_corroboration_boosts_confidence`, `test_merge_corroboration_cap`) continue to pass unmodified, proving the wildcard rule doesn't regress analyzer+agent corroboration.
4. New regression tests cover: same-line different-real-categories (must NOT merge), same-line one-real-one-other (must merge), and proximity chaining that breaks when a real-category mismatch appears mid-chain.
5. `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/` all pass.

---

## Out of Scope

- `_dedup_no_file()` (body-only/no-file text-match path) — no proximity window exists there for category to interact with; explicitly excluded by the issue's own scope ("file/line-proximity dedup").
- Any change to how `category` is populated (that's #579's scope, tracked as story 15-2 in this same epic).

---

## Technical Notes

### Current behavior (verified by reading `ai_pr_review/findings/merge.py`)

- `_dedup_file()` (lines 74-96) clusters findings sorted by line, chaining against each cluster's tail finding if `abs(tail_line - f_line) <= PROXIMITY_LINES` (`PROXIMITY_LINES = 3`, line 25). This is the **only** similarity check today — no category, severity, or text comparison.
- `_collapse_cluster()` (lines 99-114) picks the highest-severity finding as the representative (`min()` by `_SEVERITY_ORDER`), unions `sources`, and sets `corroborated=True` + boosts confidence via `provenance.py::is_corroborated()`/`boosted_confidence()` when the union spans both a native-analyzer source and an LLM-agent source. All other fields — including `category` — come straight from whichever finding won on severity.

### The wildcard requirement (why "other" must not block a merge)

`category` defaults to `"other"` (`models.py:45`) and is currently **only** populated by the 5 LLM agent prompts — every native-analyzer finding is `"other"` today purely via the model default (confirmed: none of the 13 analyzer files in `ai_pr_review/analyzers/native/` set `category` explicitly). `"other"` therefore conflates two different things: "the LLM decided this genuinely fits no category" and "no categorization signal exists at all" (analyzer default, or missing/malformed LLM output).

Treating `"other"` as blocking would cause **false-negative dedup** — the opposite of this story's goal — for the common case of an analyzer finding (always `"other"` until #579 lands) near an agent finding that self-reported a real category for the same issue. It would also break `test_merge_corroboration_boosts_confidence` / `test_merge_corroboration_cap` today, since both merge a `semgrep` finding (category defaults to `"other"`) with a `security-reviewer` finding on a nearby line and assert they collapse into one corroborated result.

**Known limitation to document in the PR, not fix here:** once #579 gives analyzers real categories, an analyzer and an agent flagging the *same* underlying issue but self-reporting genuinely *different* real categories will stop merging, losing corroboration for that pair. This is an accepted tradeoff of category-aware dedup, not a regression — call it out explicitly in the PR body so a reviewer doesn't mistake it for a bug.

### Implementation

**`_dedup_file()`** (merge.py:74-96) — add a category-compatibility check alongside the proximity check:

```python
def _category_compatible(a: Finding, b: Finding) -> bool:
    """Two findings can share a cluster unless both have differing, non-'other' categories."""
    if a.category == "other" or b.category == "other":
        return True
    return a.category == b.category
```

Change the cluster-membership condition from `abs(tail_line - f_line) <= PROXIMITY_LINES` to also require `_category_compatible(tail, f)`.

**`_collapse_cluster()`** (merge.py:99-114) — after computing `best`, also compute:
```python
non_other_categories = {f.category for f in cluster if f.category != "other"}
```
If `len(non_other_categories) == 1`, set `update["category"]` to that value (overriding whatever `best.category` happens to be). Given the wildcard rule is correctly enforced upstream, `len(non_other_categories)` can never exceed 1 within a surviving cluster.

---

## Tasks

- [x] Add `_category_compatible()` helper to `ai_pr_review/findings/merge.py`.
- [x] Wire the helper into `_dedup_file()`'s cluster-membership check.
- [x] Update `_collapse_cluster()` to preserve a non-`"other"` category from any cluster member.
- [x] Add regression tests in `tests/python/test_findings.py` (same section as existing dedup tests, lines ~271-469): different-category no-merge, one-other-wildcard merge, chaining-breaks-on-mismatch.
- [x] Extend `test_merge_corroboration_boosts_confidence` (or add a sibling) with explicit, matching non-`"other"` categories on both sides to lock in the corroboration-preserving intent.
- [x] Run `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/`.
- [ ] Open PR referencing #578; disclose the corroboration-loss tradeoff in the PR body.

---

## Dev Notes

Scoping investigation (Explore agent, full file read of `merge.py` and `test_findings.py`) confirmed: no `tests/python/findings/` nested directory exists — tests live flat in `tests/python/test_findings.py`, using a shared `_make_finding(**kw)` helper. New tests should follow that exact pattern rather than introducing a new test file.
