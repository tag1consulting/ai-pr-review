# Story 10.1: Extract Shared Partition Logic from VCS Provider post_findings

**Epic:** 10 — VCS Refactor
**Story ID:** 10-1
**Story Key:** 10-1-vcs-partition-dedup
**GitHub Issue:** #498
**Status:** ready-for-dev

---

## Story

As a **maintainer**,
I want the inline-partition, ood-split, and body-bullet rendering logic extracted out of the per-provider `post_findings` implementations,
so that bug fixes (e.g. "line not in diff" note, F<n> ID fingerprinting) only need to be applied once instead of two or three times.

---

## Acceptance Criteria

1. `partition_findings()` in `_inline.py` (already exists) is called by both `github.post_findings` and `gitlab.post_findings` instead of each provider hand-rolling its own split loop.
2. A new shared helper `split_body_findings(body: list[Finding], *, eligible_new: set[tuple[str, int]]) -> tuple[list[Finding], list[Finding]]` is added to `_inline.py`, splitting body findings into `(in_diff_body, ood_body)`. GitHub's current inline ood-split logic (`if f.out_of_diff`) moves here. GitLab gains the same split.
3. `github.post_findings` and `gitlab.post_findings` both call `partition_findings()` then `split_body_findings()` and only handle their provider-specific HTTP and payload logic.
4. Bitbucket's `post_findings` is **not changed** -- it is body-only and has no inline/ood split to share.
5. No behavior change visible to consumers: the same findings end up inline or body as before, F<n> IDs remain stable, ood findings still render in the collapsible section.
6. Full Python suite green (`pytest tests/python -q`); `mypy --strict` clean; `ruff check` clean.
7. No new config knobs, env vars, or public API surface beyond the two helpers.

---

## Implementation Tasks

- [ ] **Task 1 — Audit the exact duplication** (orient before touching anything)
  - [ ] Read `github.py:post_findings` (~line 361–580) and `gitlab.py:post_findings` (~line 414–545) side by side.
  - [ ] Confirm `partition_findings()` in `_inline.py:64` already does the inline/body split correctly (it does -- only the providers ignore it).
  - [ ] Confirm `split_body_findings` does not already exist anywhere.
  - [ ] Note what GitLab does NOT have that GitHub has: `id_map`, `assemble_id_map`, ood bullet rendering -- these are GitHub-only; do not try to generalize them.

- [ ] **Task 2 — Add `split_body_findings()` to `_inline.py`**
  - [ ] Signature: `def split_body_findings(body: list[Finding]) -> tuple[list[Finding], list[Finding]]:`
  - [ ] Returns `(in_diff, ood)` where `ood = [f for f in body if f.out_of_diff]` and `in_diff` is the complement.
  - [ ] Place after `partition_findings()` in `_inline.py`.
  - [ ] No import changes needed -- `Finding` is already imported.

- [ ] **Task 3 — Wire `github.post_findings` to call `partition_findings()`**
  - [ ] Import `partition_findings` (already importing from `_inline`; add to the existing import line at line ~19).
  - [ ] Replace the hand-rolled split loop (lines ~405–422) with a call to `partition_findings(list(findings), eligible_new=eligible_new, max_inline=max_inline)`. This returns `(inline_findings, body_findings_list)`.
  - [ ] The existing `_build_inline_comment_payload` call still runs per-finding; inline payload construction stays in GitHub.
  - [ ] Replace the ood-split inline `if f.out_of_diff` check (lines ~424–436) with a call to `split_body_findings(body_findings_list)`.
  - [ ] The rest of the method (body rendering, id_map marker, fallback paths) is untouched.

- [ ] **Task 4 — Wire `gitlab.post_findings` to call `partition_findings()`**
  - [ ] `partition_findings` is already imported at line ~33; add it to the import names.
  - [ ] Replace the hand-rolled split loop (lines ~443–469) with `partition_findings(list(findings), eligible_new=eligible_new, max_inline=max_inline)`.
  - [ ] GitLab's loop also has HTTP-fallback logic (400 → body): this is provider-specific and stays in GitLab. The partition call just gives the initial split; fallback findings are appended to body_findings after the posting loop.
  - [ ] GitLab has no ood-split or id_map -- do not add them; that is out of scope.

- [ ] **Task 5 — Tests for `split_body_findings()`**
  - [ ] Add to `tests/python/vcs/test_inline.py` (alongside existing `test_partition_findings_*` tests).
  - [ ] `test_split_body_findings_separates_ood()` -- mix of `out_of_diff=True` and `False`; assert correct split.
  - [ ] `test_split_body_findings_all_in_diff()` -- no ood findings; ood list is empty.
  - [ ] `test_split_body_findings_all_ood()` -- all ood; in_diff list is empty.
  - [ ] `test_split_body_findings_empty()` -- empty input; both lists empty.

- [ ] **Task 6 — Regression: existing provider tests must still pass without modification**
  - [ ] Run `pytest tests/python/vcs/test_github_findings.py tests/python/vcs/test_gitlab_findings.py -v` -- must be green.
  - [ ] If any existing test breaks, it indicates a behavioral difference was introduced; fix it before proceeding.

- [ ] **Task 7 — Full suite + static analysis**
  - [ ] `pytest tests/python -q` -- 1680+ tests pass.
  - [ ] `mypy --strict ai_pr_review/` -- no new errors.
  - [ ] `ruff check ai_pr_review/ tests/python/` -- clean.

---

## Dev Notes

### What already exists (do not reinvent)

| Symbol | Location | Status |
|--------|----------|--------|
| `partition_findings(findings, *, eligible_new, max_inline)` | `vcs/_inline.py:64` | EXISTS, tested, not used by providers |
| `is_inline_eligible(finding, eligible_new)` | `vcs/_inline.py:19` | EXISTS, used by GitLab directly |
| `split_body_findings(body)` | -- | DOES NOT EXIST -- create in `_inline.py` |
| `assemble_id_map`, `fingerprint` | `vcs/_finding_ids.py` | EXISTS, GitHub-only -- do not move |

### Divergences between GitHub and GitLab that must NOT be merged

- **GitHub** batches all inline comments into a single review POST (`comments: [...]`). After `partition_findings()` gives it `inline_findings`, it still builds the comment payloads via `_build_inline_comment_payload()` per finding before batching.
- **GitLab** posts each inline discussion individually via `POST /discussions`. After `partition_findings()` gives it `inline_findings`, it still loops and calls `self._build_discussion_payload()` + HTTP per finding, with per-finding HTTP fallback to body.
- These posting loops are **provider-specific and stay provider-specific**. Only the initial partitioning call is shared.
- **GitHub** has `id_map`, `assemble_id_map`, `fingerprint`, ood bullet rendering, and the id-map hidden-comment marker. None of this exists in GitLab. Do not add it; that is a separate issue.

### GitHub post_findings structure (post-refactor)

```
parse_diff_sets → eligible_new, eligible_ctx
assemble_id_map → id_map                          # GitHub-only, unchanged
partition_findings(findings, eligible_new, max_inline) → inline_findings, body_findings
# build inline comment payloads (GitHub-specific loop, unchanged)
split_body_findings(body_findings) → in_diff_body, ood_body
# format body bullets (unchanged)
# render body, id_map marker, truncate, fallback paths (unchanged)
```

### GitLab post_findings structure (post-refactor)

```
parse_diff_sets → eligible_new, eligible_ctx
partition_findings(findings, eligible_new, max_inline) → inline_findings, body_findings
# HTTP posting loop: for f in inline_findings: POST /discussions; on 400 → append to body_findings
# token_table append (unchanged)
# return FindingsResult (unchanged)
```

### Critical file locations (verified on main at 1192f38)

| File | Key lines |
|------|-----------|
| `ai_pr_review/vcs/_inline.py` | `partition_findings` at line 64; existing import of `Finding` at top |
| `ai_pr_review/vcs/github.py` | `from _inline import` at line ~19; `post_findings` at line 361; split loop at ~405–422; ood split at ~424–436 |
| `ai_pr_review/vcs/gitlab.py` | `from _inline import` at line ~33; `post_findings` at line 414; split loop at ~443–469 |
| `ai_pr_review/vcs/bitbucket.py` | `post_findings` at line 176 -- body-only, no changes |
| `tests/python/vcs/test_inline.py` | `partition_findings` tests at lines 87, 101; add `split_body_findings` tests after |
| `tests/python/vcs/test_github_findings.py` | Full provider test suite -- must pass without modification |
| `tests/python/vcs/test_gitlab_findings.py` | Full provider test suite -- must pass without modification |

### mypy gotcha

`partition_findings()` in `_inline.py` takes `list[Finding]` not `Sequence[Finding]`. GitHub and GitLab receive `Sequence[Finding]` as the method argument -- call with `list(findings)` (same pattern as the existing `assemble_id_map(prior_bodies, list(findings))` call in GitHub).

### Scope boundaries -- what is explicitly OUT of scope

- GitLab F<n> ID assignment -- separate issue, not #498.
- Bitbucket inline support -- separate issue.
- Any changes to `FindingsResult`, `VcsProvider` protocol, or orchestrator.
- Extracting the body-rendering helpers (`format_body_finding`, `join_findings`) -- they are already in `_body.py`.

### Test style (match existing)

```python
# From test_inline.py -- use same factory pattern
from ai_pr_review.findings.models import Finding

def _make_finding(**kw: object) -> Finding:
    return Finding.model_validate(
        {"severity": "High", "confidence": 80, "finding": "Test", "source": "test",
         "file": "foo.py", "line": 10} | kw
    )
```

---

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

- `ai_pr_review/vcs/_inline.py` (modified -- add `split_body_findings`)
- `ai_pr_review/vcs/github.py` (modified -- call `partition_findings`, `split_body_findings`)
- `ai_pr_review/vcs/gitlab.py` (modified -- call `partition_findings`)
- `tests/python/vcs/test_inline.py` (modified -- add `split_body_findings` tests)
