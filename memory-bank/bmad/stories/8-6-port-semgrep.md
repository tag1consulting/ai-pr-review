# Story 8.6 — Port run-semgrep.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-6
**Story Key:** 8-6-port-semgrep
**GitHub Issue:** #467
**Status:** ready-for-dev
**Batch:** 3 (MED)
**Gated on:** 8-1/8-2/8-3 (Batch 1 pattern established)

---

## User Story

As a **maintainer**, I want `run-semgrep.sh` replaced by a native Python function in `bridge.py`, so that semgrep findings are produced without spawning a bash subprocess and rule-bundle discovery is handled in Python.

---

## Acceptance Criteria

- [ ] `analyzers/run-semgrep.sh` subprocess call replaced by `_run_semgrep(changed_files, diff_file)` returning `list[Finding]`
- [ ] Rule-bundle discovery (in priority order): `SEMGREP_RULES` env var → `.semgrep/` dir → `semgrep.yml` → `--config=auto` (network fallback)
- [ ] Binary invocation: `semgrep --json --config=<config> <files...>`
- [ ] Severity map: `ERROR` → High, `WARNING` → Medium, else → Low
- [ ] `confidence = 90` on all findings
- [ ] `source = "semgrep"` on all findings
- [ ] Path normalization: none (semgrep outputs relative paths)
- [ ] Network fallback: non-zero exit → return empty list, log WARNING, do not raise
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/semgrep/` migrated to `tests/python/test_analyzer_semgrep.py`; offline mock path preserved
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_semgrep(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/semgrep.py`
2. Implement rule-bundle discovery (4 paths in priority order)
3. Run `semgrep --json --config=<config> <files>` via `subprocess.run`
4. Parse `results[]` array: `path`, `start.line`, `extra.severity`, `extra.message`
5. Handle non-zero exit (network timeout) gracefully
6. Update `_ANALYZERS` table in `bridge.py`
7. Create `tests/python/test_analyzer_semgrep.py` with offline fixture (mock network path)

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"semgrep"` |
| `confidence` | 90 |
| Severity default | Low |
| Path normalization | None |
| Network failure | Return empty, log warning |

---

## References

- Epic issue: #461
- GitHub story: #467
- Parity reference: `docs/analyzers-bash-inventory.md` — semgrep section
