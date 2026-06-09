# Story 8.11 — Port run-tflint.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-11
**Story Key:** 8-11-port-tflint
**GitHub Issue:** #472
**Status:** ready-for-dev
**Batch:** 3 (MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-tflint.sh` replaced by a native Python function in `bridge.py`, so that Terraform linting findings are produced without spawning a bash subprocess and per-directory invocation is handled in Python.

---

## Acceptance Criteria

- [ ] `analyzers/run-tflint.sh` subprocess call replaced by `_run_tflint(changed_files, diff_file)` returning `list[Finding]`
- [ ] Per-directory invocation: extract unique parent directories from changed `.tf`/`.tfvars` files; invoke tflint once per directory
- [ ] Binary invocation: `tflint --format json` (run from each module directory)
- [ ] Severity map: `error` → High, `warning` → Medium, `notice` → Low
- [ ] `confidence = 90` on all findings
- [ ] `source = "tflint"` on all findings
- [ ] Filename reconstruction: prepend directory's workspace-relative path to `range.filename`
- [ ] Deduplication: deduplicate by `file:line:finding` across multiple directory runs
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/tflint/` migrated to `tests/python/test_analyzer_tflint.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_tflint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/tflint.py`
2. Extract unique parent directories from `.tf`/`.tfvars` files
3. For each directory: run `tflint --format json` from that directory via `subprocess.run(cwd=dir)`
4. Parse `issues[]`: `rule.severity`, `message`, `range.filename`, `range.start.line`
5. Prepend `dir/` prefix to `range.filename` to produce workspace-relative path
6. Merge results; deduplicate by `(file, line, finding)`
7. Update `_ANALYZERS` table in `bridge.py`
8. Create `tests/python/test_analyzer_tflint.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"tflint"` |
| `confidence` | 90 |
| Severity default | Low |
| Path normalization | Prepend directory prefix |

---

## References

- Epic issue: #461
- GitHub story: #472
- Parity reference: `docs/analyzers-bash-inventory.md` — tflint section
