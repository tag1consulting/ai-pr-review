# Story 8.1 — Port run-shellcheck.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-1
**Story Key:** 8-1-port-shellcheck
**GitHub Issue:** #462
**Status:** ready-for-dev
**Batch:** 1 (LOW) — establishes native-analyzer pattern in bridge.py
**Gated on:** Epic 5 (#199) completion preferred but not hard-blocked

---

## User Story

As a **maintainer**, I want `run-shellcheck.sh` replaced by a native Python function in `bridge.py`, so that shellcheck findings are produced without spawning a bash subprocess and the analyzer is directly unit-testable in pytest.

---

## Acceptance Criteria

- [ ] `analyzers/run-shellcheck.sh` subprocess call in `bridge.py` replaced by `_run_shellcheck(changed_files, diff_file)` returning `list[Finding]`
- [ ] Binary invocation: `shellcheck -f json1 -S warning <files...>`
- [ ] Severity map: `error` → High, `warning` → Medium, `style`/`info` → Low
- [ ] `confidence = 95` on all findings
- [ ] `source = "shellcheck"` on all findings
- [ ] Path normalization: none (shellcheck outputs paths as passed)
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/shellcheck/` migrated to `tests/python/test_analyzer_shellcheck.py`
- [ ] Existing bats coverage (if any) remains passing or is explicitly retired
- [ ] `mypy --strict` passes on modified files
- [ ] `ruff check` passes on modified files

---

## Implementation Tasks

1. Add `_run_shellcheck(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/shellcheck.py` (new file)
2. Filter `changed_files` to `.sh`/`.bash` extensions before invoking binary
3. Run `shellcheck -f json1 -S warning <files>` via `subprocess.run`
4. Parse JSON array output; map each item to `Finding`
5. Update `_ANALYZERS` table in `bridge.py`: swap script path for the new callable
6. Create `tests/python/test_analyzer_shellcheck.py` with offline fixture tests
7. Verify `mypy` and `ruff` clean

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"shellcheck"` |
| `confidence` | 95 |
| Severity default | Low |
| Path normalization | None |

---

## References

- Epic issue: #461
- GitHub story: #462
- Epic 5 (bash deletion): #199
- E5.S6 (wrapper audit): #256
- Parity reference: `docs/analyzers-bash-inventory.md` — shellcheck section
