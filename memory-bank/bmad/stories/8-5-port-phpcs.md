# Story 8.5 — Port run-phpcs.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-5
**Story Key:** 8-5-port-phpcs
**GitHub Issue:** #466
**Status:** ready-for-dev
**Batch:** 2 (LOW-MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-phpcs.sh` replaced by a native Python function in `bridge.py`, so that PHP coding standard findings are produced without spawning a bash subprocess.

---

## Acceptance Criteria

- [ ] `analyzers/run-phpcs.sh` subprocess call replaced by `_run_phpcs(changed_files, diff_file)` returning `list[Finding]`
- [ ] Standard discovery: run `phpcs -i`; use `Drupal,DrupalPractice` if both appear, else `PSR12`
- [ ] Binary invocation: `phpcs --report=json --standard=<standard> <files...>`
- [ ] Severity map: `ERROR` → High, `WARNING` → Medium
- [ ] `confidence = 90` on all findings
- [ ] `source = "phpcs"` on all findings
- [ ] Path normalization: strip `$GITHUB_WORKSPACE/` or `$PWD/` from absolute paths
- [ ] File filter: `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/phpcs/` migrated to `tests/python/test_analyzer_phpcs.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_phpcs(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/phpcs.py`
2. Run `phpcs -i` via `subprocess.run`; check stdout for `Drupal` and `DrupalPractice`
3. Build invocation with discovered standard
4. Parse JSON output: `files` dict → `messages` array → map `type`, `message`, `line` to Finding
5. Strip workspace prefix from absolute file paths
6. Update `_ANALYZERS` table in `bridge.py`
7. Create `tests/python/test_analyzer_phpcs.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"phpcs"` |
| `confidence` | 90 |
| Severity default (unknown type) | Medium |
| Path normalization | Strip workspace/pwd prefix |

---

## References

- Epic issue: #461
- GitHub story: #466
- Parity reference: `docs/analyzers-bash-inventory.md` — phpcs section
