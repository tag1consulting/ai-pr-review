# Story 8.6: Port run-semgrep.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-semgrep.sh` replaced by a native Python function in `bridge.py`,
so that semgrep findings are produced without spawning a bash subprocess and rule-bundle discovery is handled in Python.

## Acceptance Criteria

1. `_run_semgrep(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Rule-bundle discovery (priority order): `SEMGREP_RULES` env var → `.semgrep/` dir → `semgrep.yml` → `--config=auto` (network fallback)
3. Binary invocation: `semgrep --json --config=<config> <files...>`
4. Severity map: `ERROR` → High, `WARNING` → Medium, else → Low
5. `confidence = 90` on all findings
6. `source = "semgrep"` on all findings
7. Path normalization: none (semgrep outputs relative paths)
8. Network failure (non-zero exit): return `[]`, log WARNING, do not raise
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. Fixtures from `tests/fixtures/semgrep/` migrated to `tests/python/test_analyzer_semgrep.py`; offline mock path preserved
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/semgrep.py` (AC: 1-9)
  - [ ] Implement `_discover_config() -> str` with 4-step priority chain
  - [ ] Check `shutil.which("semgrep")`; return `[]` + log WARNING if absent
  - [ ] Run `semgrep --json --config=<config> <files>` — all files from `changed_files.all_files`
  - [ ] Handle non-zero exit gracefully (network failure → `[]` + WARNING)
  - [ ] Parse `data["results"]` array; map each to Finding
  - [ ] Severity: `extra.severity` field — `"ERROR"` → High, `"WARNING"` → Medium, else → Low
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_semgrep.py` (AC: 10)
  - [ ] Test: results fixture → correct severity/confidence/source
  - [ ] Test: empty results → `[]`
  - [ ] Test: config discovery order (mock env var, mock fs paths)
  - [ ] Test: non-zero exit (network failure) → `[]`
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### Semgrep JSON output format (`semgrep --json`)

```json
{
  "results": [
    {
      "check_id": "python.lang.security.audit.exec-use.exec-use",
      "path": "src/app.py",
      "start": {"line": 10, "col": 1},
      "end": {"line": 10, "col": 20},
      "extra": {
        "severity": "ERROR",
        "message": "Use of exec()",
        "metadata": {},
        "fix": null
      }
    }
  ],
  "errors": [],
  "stats": {}
}
```
Access `data["results"]`. Fields: `path`, `start.line`, `extra.severity`, `extra.message`, `check_id`.

**Exact Finding construction:**
```python
for result in data.get("results", []):
    sev = result.get("extra", {}).get("severity", "")
    severity = "High" if sev == "ERROR" else "Medium" if sev == "WARNING" else "Low"
    Finding(
        severity=severity,
        confidence=90,
        source="semgrep",
        file=result["path"],
        line=result["start"]["line"],
        finding=f"{result['check_id']}: {result.get('extra', {}).get('message', '')}",
    )
```

**Config discovery:**
```python
def _discover_config() -> str:
    if rules := os.environ.get("SEMGREP_RULES"):
        return rules
    if Path(".semgrep").is_dir():
        return ".semgrep"
    if Path("semgrep.yml").is_file():
        return "semgrep.yml"
    return "auto"  # network fallback
```

**Network failure handling:** semgrep exits non-zero on network error when using `--config=auto`. Treat any non-zero exit code as a graceful failure (return `[]`, log WARNING). Do NOT raise an exception.

**Files to pass:** Unlike per-language analyzers, semgrep scans all changed files — use `changed_files.all_files`. Filter to files that exist on disk.

**Mock path in tests:** The bash wrapper uses `SEMGREP_MOCK_FILE`. In the Python test, mock `subprocess.run` with the fixture content — no need to replicate the env var approach, but tests must work offline.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/semgrep.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_semgrep.py`
- Fixtures: `tests/fixtures/semgrep/` — check for results/empty/error fixtures

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: tests/fixtures/semgrep/] — fixture files
- [Source: analyzers/run-semgrep.sh] — bash wrapper (reference)
- GitHub issue: #467

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
