# Story 8.4 — Port run-kube-linter.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-4
**Story Key:** 8-4-port-kube-linter
**GitHub Issue:** #465
**Status:** ready-for-dev
**Batch:** 2 (LOW-MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-kube-linter.sh` replaced by a native Python function in `bridge.py`, so that Kubernetes manifest linting is performed without spawning a bash subprocess.

---

## Acceptance Criteria

- [ ] `analyzers/run-kube-linter.sh` subprocess call replaced by `_run_kube_linter(changed_files, diff_file)` returning `list[Finding]`
- [ ] Binary invocation: `kube-linter lint --format json <eligible-files...>`
- [ ] Eligibility pre-sniff: only files containing both `apiVersion:` and `kind:` are passed to kube-linter
- [ ] All findings map to Medium severity
- [ ] `confidence = 85` on all findings
- [ ] `source = "kube-linter"` on all findings
- [ ] `line = 0` (kube-linter does not emit line numbers)
- [ ] Path normalization: none
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/kubelinter/` migrated to `tests/python/test_analyzer_kube_linter.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_kube_linter(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/kube_linter.py`
2. Filter candidate files to `.yaml`/`.yml`/`.json`
3. Content-sniff: read first 50 lines of each file; skip if `apiVersion:` or `kind:` absent
4. Run `kube-linter lint --format json <eligible-files>` via `subprocess.run`
5. Parse `Reports[].Object.Metadata.FilePath`, `Reports[].Diagnostic.Message`, `Reports[].Check`
6. All findings → Medium; confidence = 85
7. Update `_ANALYZERS` table in `bridge.py`
8. Create `tests/python/test_analyzer_kube_linter.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"kube-linter"` |
| `confidence` | 85 |
| All findings severity | Medium |
| Path normalization | None |

---

## References

- Epic issue: #461
- GitHub story: #465
- Parity reference: `docs/analyzers-bash-inventory.md` — kube-linter section
