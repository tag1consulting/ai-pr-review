# Story 8.2: Port run-ruff.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-ruff.sh` replaced by a native Python function in `bridge.py`,
so that ruff findings are produced without spawning a bash subprocess.

## Acceptance Criteria

1. `_run_ruff(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Binary invocation: `ruff check --output-format json --no-cache --exit-zero <files...>`
3. Severity map: rule code prefix `F`/`E` → High, `W`/`C` → Medium, else → Low
4. `confidence = 90` on all findings
5. `source = "ruff"` on all findings
6. Path normalization: strip `GITHUB_WORKSPACE` or `PWD` prefix from absolute paths
7. Binary-absent path: returns `[]`, logs WARNING, does not raise
8. Fixtures from `tests/fixtures/ruff/` migrated to `tests/python/test_analyzer_ruff.py`
9. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/ruff.py` (AC: 1-7)
  - [ ] Filter `changed_files.python` to `.py` files that exist on disk
  - [ ] Check `shutil.which("ruff")`; return `[]` + log WARNING if absent
  - [ ] Run `ruff check --output-format json --no-cache --exit-zero <files>` via subprocess
  - [ ] Parse flat JSON array output; map each item to Finding
  - [ ] Severity: `code[0]` in `("F","E")` → High, in `("W","C")` → Medium, else → Low
  - [ ] Strip workspace prefix: `os.environ.get("GITHUB_WORKSPACE") or os.getcwd()`
  - [ ] Remediation: `f"See {item['url']}"` if url present, else ruff docs URL
- [ ] Update `_ANALYZERS` table in `bridge.py`: replace `"run-ruff.sh"` entry
- [ ] Create `tests/python/test_analyzer_ruff.py` (AC: 8)
  - [ ] Test: F-prefix code → High/confidence=90/source="ruff"
  - [ ] Test: E-prefix code → High
  - [ ] Test: W-prefix → Medium, C-prefix → Medium
  - [ ] Test: unknown prefix → Low
  - [ ] Test: empty output → `[]`
  - [ ] Test: binary absent → `[]`
  - [ ] Test: absolute path stripped to relative
- [ ] Run mypy + ruff check (AC: 9)

## Dev Notes

### Ruff JSON output format (native `ruff check --output-format json`)

```json
[
  {
    "code": "F401",
    "filename": "app/views.py",
    "location": {"row": 3, "column": 1},
    "end_location": {"row": 3, "column": 10},
    "message": "'os' imported but unused",
    "url": "https://docs.astral.sh/ruff/rules/unused-import"
  }
]
```
This is a **flat JSON array** (unlike shellcheck's `{"comments": [...]}` wrapper). Fields: `code`, `filename`, `location.row`, `message`, `url`.

**Exact Finding construction:**
```python
prefix = item["code"][0] if item.get("code") else ""
severity = "High" if prefix in ("F", "E") else "Medium" if prefix in ("W", "C") else "Low"
Finding(
    severity=severity,
    confidence=90,
    source="ruff",
    file=_strip_workspace(item["filename"]),
    line=item["location"]["row"],
    finding=f"{item['code']}: {item['message']}",
    remediation=item.get("url") or f"See https://docs.astral.sh/ruff/rules/{item['code']}",
)
```

**Path stripping helper:**
```python
def _strip_workspace(path: str) -> str:
    workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    if path.startswith(workspace):
        path = path[len(workspace):].lstrip("/")
    return path
```

**`--exit-zero` is critical** — ruff exits non-zero when it finds issues, which would cause subprocess errors without this flag.

**Use `changed_files.python`** — pre-filtered list of `.py` files. Still check each exists on disk.

**Pattern note:** `ai_pr_review/analyzers/native/` directory was created by Story 8-1. If working in parallel, ensure `__init__.py` exists.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/ruff.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_ruff.py`
- Fixture files: `tests/fixtures/ruff/ruff-error.json`, `ruff-warning.json`, `ruff-empty.json`, `sample.py`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/analyzers/sarif.py] — reference logging/Finding pattern
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles.python] — pre-filtered Python file list
- [Source: tests/fixtures/ruff/] — ruff-error.json, ruff-warning.json, ruff-empty.json
- [Source: analyzers/run-ruff.sh] — bash wrapper (reference)
- GitHub issue: #463

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
