# Story 8.10 — Port run-eslint.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-10
**Story Key:** 8-10-port-eslint
**GitHub Issue:** #471
**Status:** ready-for-dev
**Batch:** 3 (MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-eslint.sh` replaced by a native Python function in `bridge.py`, so that JavaScript/TypeScript linting findings are produced without spawning a bash subprocess and binary/config discovery is handled in Python.

---

## Acceptance Criteria

- [ ] `analyzers/run-eslint.sh` subprocess call replaced by `_run_eslint(changed_files, diff_file)` returning `list[Finding]`
- [ ] Binary resolution (in order): `node_modules/.bin/eslint` → `npx eslint` → `eslint` on PATH; return empty if none found
- [ ] Config gate: check for `eslint.config.js`, `eslint.config.mjs`, `.eslintrc.js`, `.eslintrc.json`, `.eslintrc.yml`, `.eslintrc.yaml`; return empty if none found
- [ ] Version probe: run `eslint --version`; handle v8 vs v9 flag differences
- [ ] Severity map: severity integer `2` → High, `1` → Medium
- [ ] `confidence = 90` on all findings
- [ ] `source = "eslint"` on all findings
- [ ] Path normalization: strip workspace prefix from absolute paths
- [ ] File filter: `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`
- [ ] All fixtures from `tests/fixtures/eslint/` migrated to `tests/python/test_analyzer_eslint.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_eslint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/eslint.py`
2. Binary resolution: check paths in order using `shutil.which` and `Path.exists()`
3. Config gate: check for any eslint config file presence
4. Version probe: `subprocess.run([binary, "--version"])`, parse major version
5. Build invocation with `--format json`
6. Parse JSON array: `messages[]` → `ruleId`, `severity`, `message`, `line`
7. Strip workspace prefix
8. Update `_ANALYZERS` table in `bridge.py`
9. Create `tests/python/test_analyzer_eslint.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"eslint"` |
| `confidence` | 90 |
| Severity default | Medium |
| Path normalization | Strip workspace/pwd prefix |

---

## References

- Epic issue: #461
- GitHub story: #471
- Parity reference: `docs/analyzers-bash-inventory.md` — eslint section
