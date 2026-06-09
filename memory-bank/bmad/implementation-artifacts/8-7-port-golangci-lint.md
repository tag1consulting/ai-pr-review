# Story 8.7: Port run-golangci-lint.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-golangci-lint.sh` replaced by a native Python function in `bridge.py`,
so that Go linting findings are produced without spawning a bash subprocess and module-root discovery is handled in Python.

## Acceptance Criteria

1. `_run_golangci_lint(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. go.mod root discovery: walk up from each changed `.go` file to find nearest `go.mod`; deduplicate roots
3. Run `golangci-lint run --out-format json ./...` once per module root
4. Severity map: linter `errcheck`/`govet`/`staticcheck` → High, others → Medium
5. `confidence = 90` on all findings
6. `source = "golangci-lint"` on all findings
7. Path reconstruction: prepend module-root's workspace-relative path to `Pos.Filename`
8. Binary-absent path: returns `[]`, logs WARNING, does not raise
9. Fixtures from `tests/fixtures/golangci/` migrated to `tests/python/test_analyzer_golangci_lint.py`
10. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/golangci_lint.py` (AC: 1-8)
  - [ ] Implement `_find_go_module_root(file_path: Path) -> Path | None` — walk up looking for `go.mod`
  - [ ] Collect unique module roots from `changed_files.go`
  - [ ] Check `shutil.which("golangci-lint")`; return `[]` + log WARNING if absent
  - [ ] For each root: `subprocess.run(["golangci-lint", "run", "--out-format", "json", "./..."], cwd=root)`
  - [ ] Parse `Issues[]` array from each run; reconstruct path
  - [ ] Severity: `FromLinter` in `("errcheck", "govet", "staticcheck")` → High, else → Medium
  - [ ] Merge and deduplicate results across all roots
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_golangci_lint.py` (AC: 9)
  - [ ] Test: high-severity linter → High
  - [ ] Test: other linter → Medium
  - [ ] Test: path reconstruction (module-root prefix prepended)
  - [ ] Test: go.mod root discovery (walk-up logic)
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 10)

## Dev Notes

### golangci-lint JSON output format (`--out-format json`)

```json
{
  "Issues": [
    {
      "FromLinter": "errcheck",
      "Text": "Error return value of `rows.Scan` is not checked",
      "Severity": "error",
      "Pos": {
        "Filename": "pkg/db/query.go",
        "Offset": 1234,
        "Line": 42,
        "Column": 3
      },
      "SourceLines": ["  rows.Scan(&id)"]
    }
  ],
  "Report": {}
}
```
Access `data["Issues"]`. Fields: `FromLinter`, `Text`, `Pos.Filename`, `Pos.Line`.

**Exact Finding construction:**
```python
HIGH_LINTERS = {"errcheck", "govet", "staticcheck"}
for issue in data.get("Issues") or []:
    linter = issue.get("FromLinter", "")
    severity = "High" if linter in HIGH_LINTERS else "Medium"
    filename = issue.get("Pos", {}).get("Filename", "")
    # Prepend module-root-relative path
    rel_root = str(module_root.relative_to(Path.cwd())) if module_root != Path.cwd() else ""
    full_path = f"{rel_root}/{filename}".lstrip("/") if rel_root else filename
    Finding(
        severity=severity,
        confidence=90,
        source="golangci-lint",
        file=full_path,
        line=issue.get("Pos", {}).get("Line"),
        finding=f"{linter}: {issue.get('Text', '')}",
    )
```

**go.mod root discovery:**
```python
def _find_go_module_root(file_path: Path) -> Path | None:
    current = file_path.parent
    while True:
        if (current / "go.mod").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
```

**Running per-root:** `subprocess.run([...], cwd=str(module_root), ...)`. The `cwd` is the module directory; `Pos.Filename` is relative to that directory. Prepend the workspace-relative path to the module root to get a full repo-relative path.

**Deduplication:** Multiple `.go` files in the same module will map to the same root — deduplicate the root list before running. Also deduplicate final findings by `(file, line, finding)`.

**Exit codes:** golangci-lint exits 1 when issues found, 0 when clean. Both are valid. Exit > 1 = error.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/golangci_lint.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_golangci_lint.py`
- Fixtures: `tests/fixtures/golangci/` — check fixture files

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles.go] — Go file list
- [Source: tests/fixtures/golangci/] — fixture files
- [Source: analyzers/run-golangci-lint.sh] — bash wrapper (reference)
- GitHub issue: #468

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
