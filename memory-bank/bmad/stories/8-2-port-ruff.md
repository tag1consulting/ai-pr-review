# Story 8.2 — Port run-ruff.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-2
**Story Key:** 8-2-port-ruff
**GitHub Issue:** #463
**Status:** ready-for-dev
**Batch:** 1 (LOW) — establishes native-analyzer pattern alongside 8-1
**Gated on:** 8-1 preferred first to establish the pattern

---

## User Story

As a **maintainer**, I want `run-ruff.sh` replaced by a native Python function in `bridge.py`, so that ruff findings are produced without spawning a bash subprocess.

---

## Acceptance Criteria

- [ ] `analyzers/run-ruff.sh` subprocess call replaced by `_run_ruff(changed_files, diff_file)` returning `list[Finding]`
- [ ] Binary invocation: `ruff check --output-format json <files...>`
- [ ] Severity map: rule code prefix `F`/`E` → High, `W`/`C` → Medium, else → Low
- [ ] `confidence = 90` on all findings
- [ ] `source = "ruff"` on all findings
- [ ] Path normalization: strip `$GITHUB_WORKSPACE/` or `$PWD/` from absolute paths
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/ruff/` migrated to `tests/python/test_analyzer_ruff.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_ruff(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/ruff.py`
2. Filter to `.py` files
3. Run `ruff check --output-format json <files>` via `subprocess.run`
4. Map `filename`, `location.row`, `code`, `message` to Finding fields
5. Severity: check `code[0]` for prefix (`F`/`E` → High, `W`/`C` → Medium, else → Low)
6. Strip workspace prefix from absolute paths
7. Update `_ANALYZERS` table in `bridge.py`
8. Create `tests/python/test_analyzer_ruff.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"ruff"` |
| `confidence` | 90 |
| Severity default (unknown prefix) | Low |
| Path normalization | Strip workspace/pwd prefix |

---

## References

- Epic issue: #461
- GitHub story: #463
- Parity reference: `docs/analyzers-bash-inventory.md` — ruff section
