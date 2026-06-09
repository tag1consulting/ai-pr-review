# Story 8.7 — Port run-golangci-lint.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-7
**Story Key:** 8-7-port-golangci-lint
**GitHub Issue:** #468
**Status:** ready-for-dev
**Batch:** 3 (MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-golangci-lint.sh` replaced by a native Python function in `bridge.py`, so that Go linting findings are produced without spawning a bash subprocess and module-root discovery is handled in Python.

---

## Acceptance Criteria

- [ ] `analyzers/run-golangci-lint.sh` subprocess call replaced by `_run_golangci_lint(changed_files, diff_file)` returning `list[Finding]`
- [ ] go.mod root discovery: walk up from each changed `.go` file to find nearest `go.mod`; deduplicate roots
- [ ] Run `golangci-lint run --out-format json ./...` once per module root
- [ ] Severity map: linter name `errcheck`/`govet`/`staticcheck` → High, others → Medium
- [ ] `confidence = 90` on all findings
- [ ] `source = "golangci-lint"` on all findings
- [ ] Path reconstruction: prepend module-root's workspace-relative path to `Pos.Filename`
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/golangci/` migrated to `tests/python/test_analyzer_golangci_lint.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_golangci_lint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/golangci_lint.py`
2. Implement `_find_go_module_root(file_path: Path) -> Path | None` — walk up directories looking for `go.mod`
3. Deduplicate roots; derive package patterns from changed files per root
4. Run `golangci-lint run --out-format json ./...` from each root
5. Parse `Issues[]`: `FromLinter`, `Text`, `Pos.Filename`, `Pos.Line`
6. Prepend module-root prefix to `Pos.Filename`
7. Update `_ANALYZERS` table in `bridge.py`
8. Create `tests/python/test_analyzer_golangci_lint.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"golangci-lint"` |
| `confidence` | 90 |
| Severity default | Medium |
| Path normalization | Prepend module-root prefix |

---

## References

- Epic issue: #461
- GitHub story: #468
- Parity reference: `docs/analyzers-bash-inventory.md` — golangci-lint section
