# Story 8.10: Port run-eslint.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-eslint.sh` replaced by a native Python function in `bridge.py`,
so that JavaScript/TypeScript linting findings are produced without spawning a bash subprocess and binary/config discovery is handled in Python.

## Acceptance Criteria

1. `_run_eslint(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Binary resolution (in order): `node_modules/.bin/eslint` → `npx eslint` → `eslint` on PATH; return `[]` if none found
3. Config gate: check for any eslint config file; return `[]` if none found
4. Severity map: integer `2` → High, `1` → Medium
5. `confidence = 90` on all findings
6. `source = "eslint"` on all findings
7. Path normalization: strip workspace prefix from absolute paths
8. File filter: `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. Fixtures from `tests/fixtures/eslint/` migrated to `tests/python/test_analyzer_eslint.py`
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/eslint.py` (AC: 1-9)
  - [ ] Implement `_resolve_eslint_binary() -> list[str] | None` — returns argv prefix or None
  - [ ] Implement `_has_eslint_config() -> bool` — check all config file names
  - [ ] Filter `changed_files.js_ts` + other JS/TS extensions from `changed_files.all_files`
  - [ ] Run `<binary> --format json <files>` via subprocess
  - [ ] Parse JSON array output: each item has `filePath` and `messages[]`
  - [ ] Map `messages[].severity` (int), `messages[].message`, `messages[].line`, `messages[].ruleId`
  - [ ] Strip workspace prefix from `filePath`
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_eslint.py` (AC: 10)
  - [ ] Test: severity 2 → High; severity 1 → Medium
  - [ ] Test: binary resolution order
  - [ ] Test: config gate (no config → `[]`)
  - [ ] Test: path stripping
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### ESLint JSON output format (`eslint --format json`)

```json
[
  {
    "filePath": "/workspace/src/app.js",
    "messages": [
      {
        "ruleId": "no-unused-vars",
        "severity": 2,
        "message": "'x' is defined but never used.",
        "line": 5,
        "column": 3,
        "endLine": 5,
        "endColumn": 4,
        "fix": null
      }
    ],
    "errorCount": 1,
    "warningCount": 0,
    "fixableErrorCount": 0,
    "fixableWarningCount": 0
  }
]
```
Array of file objects. Each has `filePath` and `messages[]`. Severity is an integer: `2` = error/High, `1` = warning/Medium.

**Binary resolution:**
```python
def _resolve_eslint_binary() -> list[str] | None:
    local = Path("node_modules/.bin/eslint")
    if local.exists():
        return [str(local)]
    if shutil.which("npx"):
        return ["npx", "eslint"]
    if shutil.which("eslint"):
        return ["eslint"]
    return None
```

**Config gate — check for any of these files:**
```python
_ESLINT_CONFIGS = [
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
    ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.yaml", ".eslintrc.yml",
    ".eslintrc.json", ".eslintrc",
]
def _has_eslint_config() -> bool:
    return any(Path(c).exists() for c in _ESLINT_CONFIGS)
```

**Exact Finding construction:**
```python
for file_result in data:
    file_path = _strip_workspace(file_result.get("filePath", ""))
    for msg in file_result.get("messages", []):
        sev = msg.get("severity", 1)
        severity = "High" if sev == 2 else "Medium"
        rule_id = msg.get("ruleId") or ""
        message = msg.get("message", "")
        finding_text = f"{rule_id}: {message}" if rule_id else message
        Finding(
            severity=severity,
            confidence=90,
            source="eslint",
            file=file_path,
            line=msg.get("line"),
            finding=finding_text,
        )
```

**File extensions:** `changed_files.js_ts` covers `.js`/`.jsx`/`.ts`/`.tsx`. Also filter for `.mjs`/`.cjs` from `changed_files.all_files` if not already in `js_ts`.

**Exit codes:** eslint exits 1 when linting errors found, 0 when clean. Both valid. Exit 2 = fatal error.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/eslint.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_eslint.py`
- Fixtures: `tests/fixtures/eslint/`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table; eslint gated on `["js_ts"]`
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles.js_ts] — JS/TS file list
- [Source: tests/fixtures/eslint/] — fixture files
- [Source: analyzers/run-eslint.sh] — bash wrapper (reference)
- GitHub issue: #471

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
