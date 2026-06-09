# Story 8.3: Port run-hadolint.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-hadolint.sh` replaced by a native Python function in `bridge.py`,
so that Dockerfile linting findings are produced without spawning a bash subprocess.

## Acceptance Criteria

1. `_run_hadolint(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Binary invocation: `hadolint --format json <files...>`
3. File filter: `Dockerfile*` and `*.dockerfile` only
4. Severity map: `error` → High, `warning` → Medium, `info`/`style` → Low
5. `confidence = 90` on all findings
6. `source = "hadolint"` on all findings
7. Path normalization: none
8. Binary-absent path: returns `[]`, logs WARNING, does not raise
9. Fixtures from `tests/fixtures/hadolint/` migrated to `tests/python/test_analyzer_hadolint.py`
10. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/hadolint.py` (AC: 1-8)
  - [ ] Filter `changed_files.dockerfile` to files matching `Dockerfile*` or `*.dockerfile`
  - [ ] Check `shutil.which("hadolint")`; return `[]` + log WARNING if absent
  - [ ] Run `hadolint --format json <files>` via subprocess (all eligible files in one call)
  - [ ] Parse JSON array output; map each item to Finding
  - [ ] Severity: `"error"` → High, `"warning"` → Medium, else → Low
- [ ] Update `_ANALYZERS` table in `bridge.py`: replace `"run-hadolint.sh"` entry
- [ ] Create `tests/python/test_analyzer_hadolint.py` (AC: 9)
  - [ ] Test: error fixture → High
  - [ ] Test: warning fixture → Medium
  - [ ] Test: info fixture → Low
  - [ ] Test: empty → `[]`
  - [ ] Test: binary absent → `[]`
  - [ ] Test: malformed JSON → `[]`
- [ ] Run mypy + ruff check (AC: 10)

## Dev Notes

### Hadolint JSON output format (`hadolint --format json`)

```json
[
  {
    "file": "Dockerfile",
    "line": 3,
    "column": 1,
    "level": "warning",
    "code": "DL3008",
    "message": "Pin versions in apt get install.",
    "url": "https://github.com/hadolint/hadolint/wiki/DL3008"
  }
]
```
Flat JSON array. Fields: `file`, `line`, `level`, `code`, `message`, `url`.

**Exact Finding construction:**
```python
severity_map = {"error": "High", "warning": "Medium"}
Finding(
    severity=severity_map.get(item["level"], "Low"),
    confidence=90,
    source="hadolint",
    file=item["file"],
    line=item["line"],
    finding=f"{item['code']}: {item['message']}",
    remediation=item.get("url", ""),
)
```

**File filtering:** `changed_files.dockerfile` from `ChangedFiles` already contains Dockerfile paths. Still apply the name pattern filter `basename.startswith("Dockerfile") or basename.endswith(".dockerfile")` in case the `dockerfile` list includes other types.

**All files in one call:** Unlike shellcheck (per-file loop), pass all eligible Dockerfile paths in a single `hadolint --format json file1 file2 ...` invocation.

**Exit code:** hadolint exits non-zero when it finds issues. Use `subprocess.run(..., check=False)` and handle returncode `> 1` as an error. Exit codes 0 and 1 are both valid (0 = no findings, 1 = findings found).

**`native/` directory:** Created by Story 8-1. Ensure `__init__.py` exists.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/hadolint.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_hadolint.py`
- Fixtures: `tests/fixtures/hadolint/hadolint-error.json`, `hadolint-warning.json`, `hadolint-info.json`, `hadolint-empty.json`, `hadolint-malformed.json`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/analyzers/sarif.py] — reference logging/Finding pattern
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles.dockerfile] — pre-filtered Dockerfile list
- [Source: tests/fixtures/hadolint/] — fixture files
- [Source: analyzers/run-hadolint.sh] — bash wrapper (reference)
- GitHub issue: #464

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
