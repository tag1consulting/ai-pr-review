# Story 8.9 — Port run-phpstan.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-9
**Story Key:** 8-9-port-phpstan
**GitHub Issue:** #470
**Status:** ready-for-dev
**Batch:** 3 (MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-phpstan.sh` replaced by a native Python function in `bridge.py`, so that PHP static analysis findings are produced without spawning a bash subprocess and config/autoload discovery is handled in Python.

---

## Acceptance Criteria

- [ ] `analyzers/run-phpstan.sh` subprocess call replaced by `_run_phpstan(changed_files, diff_file)` returning `list[Finding]`
- [ ] Config discovery: check for `phpstan.neon` or `phpstan.neon.dist` in workspace root
- [ ] Autoload: add `--autoload-file=vendor/autoload.php` if `vendor/autoload.php` exists
- [ ] Level: honor `PHPSTAN_LEVEL` env var (default 3); overridden by config file
- [ ] Binary invocation: `phpstan analyse --error-format=json --level=<N> [--configuration=<path>] [--autoload-file=...] <files...>`
- [ ] All findings map to **High** severity (phpstan has no severity levels)
- [ ] `confidence = 85` on all findings
- [ ] `source = "phpstan"` on all findings
- [ ] Path normalization: strip `$GITHUB_WORKSPACE/` or `$PWD/` from absolute paths
- [ ] File filter: `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/phpstan/` migrated to `tests/python/test_analyzer_phpstan.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_phpstan(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/phpstan.py`
2. Config discovery: `Path("phpstan.neon").exists()` or `Path("phpstan.neon.dist").exists()`
3. Autoload: `Path("vendor/autoload.php").exists()`
4. Build invocation with level/config/autoload args
5. Parse JSON output: `files` dict → `errors[]` → `line`, `message`
6. All findings → High; confidence = 85
7. Strip workspace prefix from absolute paths
8. Update `_ANALYZERS` table in `bridge.py`
9. Create `tests/python/test_analyzer_phpstan.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"phpstan"` |
| `confidence` | 85 |
| All findings severity | **High** |
| Path normalization | Strip workspace/pwd prefix |

---

## References

- Epic issue: #461
- GitHub story: #470
- Parity reference: `docs/analyzers-bash-inventory.md` — phpstan section
