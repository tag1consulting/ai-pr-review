# Story 8.5: Port run-phpcs.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-phpcs.sh` replaced by a native Python function in `bridge.py`,
so that PHP coding standard findings are produced without spawning a bash subprocess.

## Acceptance Criteria

1. `_run_phpcs(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Standard discovery: run `phpcs -i`; use `Drupal,DrupalPractice` if both present, else `PSR12`
3. Binary invocation: `phpcs --report=json --standard=<standard> <files...>`
4. Severity map: `ERROR` → High, `WARNING` → Medium
5. `confidence = 90` on all findings
6. `source = "phpcs"` on all findings
7. Path normalization: strip `GITHUB_WORKSPACE` or `PWD` prefix from absolute paths
8. File filter: `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. Fixtures from `tests/fixtures/phpcs/` migrated to `tests/python/test_analyzer_phpcs.py`
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/phpcs.py` (AC: 1-9)
  - [ ] Filter `changed_files.php` to PHP extensions: `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`
  - [ ] Check `shutil.which("phpcs")`; return `[]` + log WARNING if absent
  - [ ] Run `phpcs -i` to discover installed standards; check stdout for `Drupal` and `DrupalPractice`
  - [ ] Select standard: `"Drupal,DrupalPractice"` if both present, else `"PSR12"`
  - [ ] Run `phpcs --report=json --standard=<standard> <files>` via subprocess
  - [ ] Parse JSON: `data["files"]` dict → iterate values → `messages[]` array
  - [ ] Strip workspace prefix from absolute file paths (from the `files` dict keys)
  - [ ] Severity: `"ERROR"` → High, else → Medium
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_phpcs.py` (AC: 10)
  - [ ] Test: error fixture → High/confidence=90/source="phpcs"
  - [ ] Test: warning fixture → Medium
  - [ ] Test: empty fixture → `[]`
  - [ ] Test: malformed fixture → `[]`
  - [ ] Test: standard selection (Drupal present → Drupal standard)
  - [ ] Test: standard selection (no Drupal → PSR12)
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### phpcs JSON output format (`phpcs --report=json`)

```json
{
  "totals": {"errors": 1, "warnings": 0, "fixable": 0},
  "files": {
    "/workspace/src/MyClass.php": {
      "errors": 1,
      "warnings": 0,
      "messages": [
        {
          "message": "Missing doc comment for function myFunc()",
          "source": "Drupal.Commenting.FunctionComment.Missing",
          "severity": 5,
          "fixable": false,
          "type": "ERROR",
          "line": 42,
          "column": 3
        }
      ]
    }
  }
}
```
The `files` key is a dict keyed by absolute file path. Each value has `messages[]` with `type` (ERROR/WARNING), `message`, `line`.

**Exact Finding construction:**
```python
for abs_path, file_data in data.get("files", {}).items():
    rel_path = _strip_workspace(abs_path)
    for msg in file_data.get("messages", []):
        severity = "High" if msg.get("type") == "ERROR" else "Medium"
        Finding(
            severity=severity,
            confidence=90,
            source="phpcs",
            file=rel_path,
            line=msg["line"],
            finding=msg["message"],
        )
```

**Standard discovery:**
```python
result = subprocess.run(["phpcs", "-i"], capture_output=True, text=True)
installed = result.stdout
has_drupal = "Drupal" in installed and "DrupalPractice" in installed
standard = "Drupal,DrupalPractice" if has_drupal else "PSR12"
```

**File extensions:** `changed_files.php` contains `.php` files, but PHP extensions in this project also include `.module`, `.inc`, `.theme`, `.install`, `.profile`. Filter `changed_files.all_files` by these extensions, not just `changed_files.php` (which only tracks `.php`).

**Path strip helper:** Same pattern as ruff:
```python
workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
if abs_path.startswith(workspace):
    rel = abs_path[len(workspace):].lstrip("/")
```

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/phpcs.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_phpcs.py`
- Fixtures: `tests/fixtures/phpcs/phpcs-error.json`, `phpcs-warning.json`, `phpcs-empty.json`, `phpcs-malformed.json`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles.php] — PHP file list (`.php` only)
- [Source: tests/fixtures/phpcs/] — fixture files
- [Source: analyzers/run-phpcs.sh] — bash wrapper (reference)
- GitHub issue: #466

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
