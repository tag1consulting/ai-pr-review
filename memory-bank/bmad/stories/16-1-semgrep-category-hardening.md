# Story 16.1: Harden Semgrep Category-Mapping Substring Heuristic

**Epic:** 16 — Semgrep category-mapping hardening
**Story ID:** 16-1
**Story Key:** 16-1-semgrep-category-hardening
**GitHub Issue:** [#585](https://github.com/tag1consulting/ai-pr-review/issues/585)
**Status:** done

---

## Story

As a **maintainer**,
I want `ai_pr_review/analyzers/native/semgrep.py`'s `_map_category()` to anchor its `check_id` substring hints instead of using bare substring containment, with test coverage for every hint category and the check_id-vs-metadata precedence rule,
so that a rule ID like `python.lang.sqlite-config` doesn't get mis-tagged `injection` (via the `"sqli"` fragment) when it has nothing to do with SQL injection.

---

## Acceptance Criteria

1. `_CHECK_ID_CATEGORY_HINTS` fragments are matched with a delimiter-aware boundary check (fragment must be bounded by `.`, `-`, `_`, or start/end of string on both sides) instead of `fragment in lower_id` substring containment.
2. A `check_id` containing `sqlite-config` (or any other rule name that happens to contain a hint fragment as a false-positive substring) does NOT map to the fragment's category when the fragment isn't a delimiter-bounded token.
3. All 13 existing `_CHECK_ID_CATEGORY_HINTS` fragments continue to match their intended real-world semgrep rule ID shapes (e.g. `python.lang.security.audit.sql-injection` still maps to `injection`) — the anchoring must not become so strict it breaks genuine matches.
4. New tests cover the previously-untested `secret` and `authz` check_id-hint branches (only `injection` had a test before this story).
5. A new test pins `metadata.category == "security"` → `"other"` (currently only documented via a code comment, not tested).
6. A new test locks in check_id-hint-vs-metadata precedence: when both a matching check_id fragment and a conflicting `metadata.category` are present on the same finding, the check_id hint wins (matches the current, unchanged code order — this test guards against a future accidental reordering).
7. `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/` all pass.

---

## Out of Scope

- Any change to `ai_pr_review/findings/merge.py`'s dedup/clustering logic — unrelated, already shipped in #578/PR #584.
- Any change to other analyzers' category mappings (checkov, kube_linter, etc.) — those are #579/PR #584, already shipped.
- Expanding `_CHECK_ID_CATEGORY_HINTS` or `_METADATA_CATEGORY_MAP` with new fragments/categories — this story hardens the existing mapping mechanism, it does not extend its coverage.

---

## Technical Notes

### Current behavior (verified by reading `ai_pr_review/analyzers/native/semgrep.py:25-79`)

`_map_category(check_id, metadata)`:
1. Lowercases `check_id`, then checks each of 13 `_CHECK_ID_CATEGORY_HINTS` fragments via `if fragment in lower_id: return category` — bare substring containment, in priority order.
2. Falls back to `metadata.get("category")`, mapped through `_METADATA_CATEGORY_MAP` (6 entries).
3. Returns `"other"` if neither yields a match.

### The false-positive risk (confirmed, not theoretical)

Semgrep rule IDs are dot/dash-delimited (e.g. `python.lang.security.audit.subprocess-shell-true`). The `"sqli"` fragment (intended for `sql-injection`/`sqli`-named rules, mapped to `injection`) is a substring of `sqlite`, so a rule ID like `python.lang.sqlite-config` — nothing to do with SQL injection — currently gets mis-tagged `injection` via `"sqli" in "python.lang.sqlite-config"` evaluating true. Verified directly against the current code:

```python
>>> "sqli" in "python.lang.sqlite-config".lower()
True
```

This is non-regressive today (every semgrep finding was `"other"` before #579 shipped `_map_category`), but real: `sqlite`, `sqlite3`, and similar rule-name fragments are plausible in a Python/database-heavy ruleset, and a mis-tag both misleads a human reader of the category field and (via #578's category-aware dedup in `merge.py`) can cause a lost corroboration opportunity when the mis-tagged category no longer matches a paired agent finding's real category.

### Anchoring approach

Semgrep rule IDs use `.`, `-`, and occasionally `_` as component delimiters. A fragment should only match when it appears as a complete delimited token — bounded by one of `.-_` (or start/end of string) on **both** sides. Regex-based approach, built once at module load time from the existing `_CHECK_ID_CATEGORY_HINTS` tuple:

```python
import re

_DELIM = r"[.\-_]"

def _compile_hint_pattern(fragment: str) -> re.Pattern[str]:
    escaped = re.escape(fragment)
    return re.compile(rf"(?:^|{_DELIM}){escaped}(?:{_DELIM}|$)")

_COMPILED_HINTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (_compile_hint_pattern(fragment), category)
    for fragment, category in _CHECK_ID_CATEGORY_HINTS
)
```

Then in `_map_category`, replace `if fragment in lower_id` with `if pattern.search(lower_id)` iterating `_COMPILED_HINTS` instead of `_CHECK_ID_CATEGORY_HINTS` directly.

**Verify against real rule ID shapes before finalizing** — multi-word fragments like `"sql-injection"`, `"command-injection"`, `"path-traversal"`, `"access-control"` are themselves dash-joined; the anchoring regex must treat the fragment as one bounded unit (which `re.escape(fragment)` + boundary check already does correctly, since the dash *inside* the fragment is part of the escaped literal, not a boundary point). Confirm with a quick manual check for each of the 13 fragments against at least one real-shaped semgrep rule ID before relying on the test suite alone.

### Precedence test design

`_map_category` checks `_CHECK_ID_CATEGORY_HINTS`/`_COMPILED_HINTS` first, unconditionally returning on the first match — `metadata.category` is only consulted if no check_id fragment matched. A test with a check_id containing a real fragment (e.g. `"sql-injection"`) AND `metadata={"category": "correctness"}` must assert the result is `"injection"` (check_id wins), not `"lint"` (which would be the outcome if metadata were checked first). This locks in the current, correct order and would fail if a future refactor accidentally swapped the two lookups.

---

## Tasks

- [x] Add delimiter-aware anchoring (`_compile_hint_pattern` / `_COMPILED_HINTS`) to `ai_pr_review/analyzers/native/semgrep.py`, replacing bare substring containment in `_map_category`.
- [x] Manually verify all 13 existing hint fragments still match a real-shaped semgrep check_id example each (or add/adjust as needed if any anchoring interaction is found). Verified via standalone script: all 13 positive cases matched, all 3 negative/false-positive cases (including the exact `sqlite-config` example plus `author`/`secretary` contrived cases) correctly did not match.
- [x] Add a regression test proving the `sqlite-config` false positive is fixed (asserts NOT `"injection"`, i.e. falls through to `"other"` or a correctly-matched category).
- [x] Add tests for the untested `secret` and `authz` check_id-hint branches.
- [x] Add a test pinning `metadata.category == "security"` → `"other"`.
- [x] Add a check_id-hint-vs-metadata precedence test.
- [x] Run `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/`. 1811 tests pass, mypy clean (pre-existing baseline error only), ruff clean.
- [x] **Follow-up fix (found during `/comprehensive-review --quick`, independently confirmed by two review agents plus advisor):** the first anchoring pass required a trailing delimiter on every fragment, which broke real-world inflected/pluralized forms for the 4 stem-shaped fragments (`secret`, `credential`, `auth`, `privilege`) — e.g. `generic.secrets.security.detected-private-key`, `python.django.security.audit.authorization-bypass`, and `java.lang.security.audit.authentication-bypass` all regressed to `"other"` instead of `"secret"`/`"authz"`. Fixed by adding explicit whole-token variants for each real inflected form (`secrets`, `credentials`, `authentication`, `unauthenticated`, `authorization`, `unauthorized`, `authorized`, `authn`, `privileges`, `privileged`) rather than relaxing the trailing-boundary rule (which would have reopened `auth`-inside-`author` / `secret`-inside-`secretary` false positives). Also found and fixed: 2 of the original new tests (`test_category_maps_secret_check_id_to_secret`, `test_category_maps_authz_check_id_to_authz`) passed for the wrong reason — their check_ids routed through a different fragment (`hardcoded`, `privilege`) than the one the test name claimed to cover. Re-pointed both at check_ids that isolate the claimed fragment, and added a parametrized regression test covering all 10 inflected forms plus 2 tests locking in that `auth`/`secret` still don't false-positive on `author`/`secretary`. Full suite re-verified: 1823 tests pass, mypy clean (same pre-existing baseline error), ruff clean.
- [x] **Second follow-up (found by advisor on re-review of the first follow-up):** the same stem-collision class also affected two forms not yet in the list: `oauth` (auth with a left-side prefix, no delimiter) and `authz` (semgrep rule packs sometimes use this as the category name directly, e.g. `spring.security.authz-check`). Added both as explicit tokens; extended the parametrized regression test to cover them. Full suite: 1825 tests pass, mypy/ruff unchanged. The fragment list is now documented in-code as a best-effort enumeration of known naming conventions, not an exhaustive derivation — a future unlisted form falls through to `"other"` (safe) rather than false-positiving, but won't get the specific category either; extend the list if a real-world gap like this is found again.
- [ ] Open PR referencing #585.

---

## Dev Notes

Scoping investigation (this session, full read of `ai_pr_review/analyzers/native/semgrep.py` and `tests/python/test_analyzer_semgrep.py`) confirmed: only 1 of 13 check_id-hint fragments (`sql-injection`, tested indirectly via the `injection` category) and 1 of 6 metadata-map entries (`correctness`) have any test coverage today. This story closes that specific gap alongside the anchoring fix, per #585's stated scope. `_map_category`'s fallback-to-`"other"` behavior on no match is unchanged and already covered by `test_category_falls_back_to_other_when_unclassified` — no new test needed for that path.

**Post-implementation correction:** the initial anchoring design (delimiter required on both sides of every fragment) was verified against one contrived real-shaped example per fragment, which happened to avoid the plural/inflected forms that are actually the dominant real-world shape for the `secret` and `authz` categories (semgrep's own `generic.secrets.*` rule namespace, and `authorization`/`authentication`/`authn` in Django/Flask/Spring rule packs). `/comprehensive-review --quick`'s code-reviewer and pr-test-analyzer agents both independently found this gap, and the advisor tool flagged it as blocking before the PR was opened — all three arrived at the same 4 fragments and largely the same real-world check_id examples. This is now fixed via explicit inflected-form variants rather than loosening the anchor (see Task list above and Technical Notes' updated fragment table for the reasoning on why a purely structural fix can't distinguish `authorization` (wanted) from `author` (not wanted) — the distinction is semantic and has to live in the fragment list).
