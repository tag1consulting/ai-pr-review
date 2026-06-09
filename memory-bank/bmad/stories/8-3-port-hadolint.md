# Story 8.3 — Port run-hadolint.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-3
**Story Key:** 8-3-port-hadolint
**GitHub Issue:** #464
**Status:** ready-for-dev
**Batch:** 1 (LOW) — establishes native-analyzer pattern alongside 8-1/8-2
**Gated on:** 8-1 preferred first to establish the pattern

---

## User Story

As a **maintainer**, I want `run-hadolint.sh` replaced by a native Python function in `bridge.py`, so that Dockerfile linting findings are produced without spawning a bash subprocess.

---

## Acceptance Criteria

- [ ] `analyzers/run-hadolint.sh` subprocess call replaced by `_run_hadolint(changed_files, diff_file)` returning `list[Finding]`
- [ ] Binary invocation: `hadolint --format json <files...>`
- [ ] Severity map: `error` → High, `warning` → Medium, `info`/`style` → Low
- [ ] `confidence = 90` on all findings
- [ ] `source = "hadolint"` on all findings
- [ ] Path normalization: none (hadolint outputs paths as passed)
- [ ] File filter: `Dockerfile*` and `*.dockerfile` only
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/hadolint/` migrated to `tests/python/test_analyzer_hadolint.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_hadolint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/hadolint.py`
2. Filter to `Dockerfile*` / `*.dockerfile` files
3. Run `hadolint --format json <files>` via `subprocess.run`
4. Map `file`, `line`, `level`, `code`, `message` to Finding fields
5. Severity: `error` → High, `warning` → Medium, else → Low
6. Update `_ANALYZERS` table in `bridge.py`
7. Create `tests/python/test_analyzer_hadolint.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"hadolint"` |
| `confidence` | 90 |
| Severity default | Low |
| Path normalization | None |

---

## References

- Epic issue: #461
- GitHub story: #464
- Parity reference: `docs/analyzers-bash-inventory.md` — hadolint section
