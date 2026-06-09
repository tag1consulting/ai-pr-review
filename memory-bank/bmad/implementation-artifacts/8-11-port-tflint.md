# Story 8.11: Port run-tflint.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-tflint.sh` replaced by a native Python function in `bridge.py`,
so that Terraform linting findings are produced without spawning a bash subprocess and per-directory invocation is handled in Python.

## Acceptance Criteria

1. `_run_tflint(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Per-directory invocation: extract unique parent directories from changed `.tf`/`.tfvars` files; invoke tflint once per directory
3. Binary invocation: `tflint --format json` (run from each module directory via `cwd=`)
4. Severity map: `error` → High, `warning` → Medium, `notice` → Low
5. `confidence = 90` on all findings
6. `source = "tflint"` on all findings
7. Filename reconstruction: prepend directory's workspace-relative path to `range.filename`
8. Deduplication: deduplicate by `(file, line, finding)` across multiple directory runs
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. Fixtures from `tests/fixtures/tflint/` migrated to `tests/python/test_analyzer_tflint.py`
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/tflint.py` (AC: 1-9)
  - [ ] Extract unique parent directories from `changed_files.terraform`
  - [ ] Check `shutil.which("tflint")`; return `[]` + log WARNING if absent
  - [ ] For each directory: `subprocess.run(["tflint", "--format", "json"], cwd=str(dir_path))`
  - [ ] Parse `issues[]` array from each run
  - [ ] Map `rule.severity`, `message`, `range.filename`, `range.start.line`
  - [ ] Prepend `dir/` prefix to `range.filename` for workspace-relative path
  - [ ] Merge all results; deduplicate by `(file, line, finding_text)`
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_tflint.py` (AC: 10)
  - [ ] Test: error → High, warning → Medium, notice → Low
  - [ ] Test: path reconstruction (dir prefix prepended)
  - [ ] Test: deduplication across multiple dir runs
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### tflint JSON output format (`tflint --format json`)

```json
{
  "issues": [
    {
      "rule": {
        "name": "terraform_deprecated_interpolation",
        "severity": "warning",
        "link": "https://github.com/terraform-linters/tflint-ruleset-terraform/blob/v0.1.0/docs/rules/terraform_deprecated_interpolation.md"
      },
      "message": "Interpolation-only expressions are deprecated in Terraform v0.12.14",
      "range": {
        "filename": "main.tf",
        "start": {"line": 5, "column": 13},
        "end": {"line": 5, "column": 27}
      }
    }
  ],
  "errors": []
}
```
Note: `range.filename` is relative to the directory tflint was run from (the module directory), NOT workspace-relative.

**Exact Finding construction:**
```python
# dir_path is the module directory as a workspace-relative Path
for issue in data.get("issues", []):
    rule = issue.get("rule", {})
    sev_str = rule.get("severity", "notice")
    severity = {"error": "High", "warning": "Medium"}.get(sev_str, "Low")
    filename = issue.get("range", {}).get("filename", "")
    # Reconstruct workspace-relative path
    rel_dir = str(dir_path.relative_to(Path.cwd())) if dir_path != Path.cwd() else ""
    full_path = f"{rel_dir}/{filename}".lstrip("/") if rel_dir else filename
    line = issue.get("range", {}).get("start", {}).get("line")
    Finding(
        severity=severity,
        confidence=90,
        source="tflint",
        file=full_path,
        line=line,
        finding=issue.get("message", ""),
        remediation=rule.get("link", ""),
    )
```

**Per-directory logic:**
```python
dirs = {Path(f).parent for f in changed_files.terraform if Path(f).parent.exists()}
# dirs are relative to cwd; pass as cwd= to subprocess
```

**Deduplication across dirs:** A finding may appear in multiple dir runs if configs overlap. Deduplicate by `(file, line, finding_text)` using a set.

**Exit codes:** tflint exits 0 = no issues, 1 = issues found, 2 = fatal error. Treat 0 and 1 as valid.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/tflint.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_tflint.py`
- Fixtures: `tests/fixtures/tflint/`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table; tflint gated on `["terraform"]`
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles.terraform] — Terraform file list
- [Source: tests/fixtures/tflint/] — fixture files
- [Source: analyzers/run-tflint.sh] — bash wrapper (reference)
- GitHub issue: #472

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
