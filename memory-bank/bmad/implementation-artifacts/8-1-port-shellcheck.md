# Story 8.1: Port run-shellcheck.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-shellcheck.sh` replaced by a native Python function in `bridge.py`,
so that shellcheck findings are produced without spawning a bash subprocess and the analyzer is directly unit-testable in pytest.

## Acceptance Criteria

1. `_run_shellcheck(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Binary invocation: `shellcheck -f json1 -S warning <files...>` (per-file loop)
3. Severity map: `error` → High, `warning` → Medium, `style`/`info` → Low
4. `confidence = 95` on all findings
5. `source = "shellcheck"` on all findings
6. Finding text: `SC<code>: <message>`; remediation: `See https://www.shellcheck.net/wiki/SC<code>`
7. Path normalization: none (shellcheck outputs paths as passed)
8. Binary-absent path: returns `[]`, logs WARNING, does not raise
9. Fixtures from `tests/fixtures/shellcheck/` migrated to `tests/python/test_analyzer_shellcheck.py`
10. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/shellcheck.py` (AC: 1-8)
  - [ ] Filter `changed_files.shell` to `.sh`/`.bash` extensions that exist on disk
  - [ ] Check `shutil.which("shellcheck")`; return `[]` + log WARNING if absent
  - [ ] Loop per file: `subprocess.run(["shellcheck", "-f", "json1", "-S", "warning", file], ...)`
  - [ ] Parse `data["comments"]` array; map each item to Finding
  - [ ] Severity: `"error"` → `"High"`, `"warning"` → `"Medium"`, else → `"Low"`
  - [ ] Build `finding = f"SC{item['code']}: {item['message']}"` and `remediation` URL
  - [ ] Handle missing binary (FileNotFoundError / OSError) gracefully
- [ ] Create `ai_pr_review/analyzers/native/__init__.py` (empty, if directory is new)
- [ ] Update `_ANALYZERS` table in `bridge.py`: replace `"run-shellcheck.sh"` entry (AC: 1)
- [ ] Create `tests/python/test_analyzer_shellcheck.py` (AC: 9)
  - [ ] Test: warning fixture → Medium/confidence=95/source="shellcheck"
  - [ ] Test: error fixture → High
  - [ ] Test: style/info → Low
  - [ ] Test: empty fixture → `[]`
  - [ ] Test: shellcheck binary absent → `[]` (mock shutil.which)
  - [ ] Test: malformed JSON → `[]` (graceful)
- [ ] Run `mypy --strict ai_pr_review/analyzers/native/shellcheck.py` (AC: 10)
- [ ] Run `ruff check ai_pr_review/analyzers/native/shellcheck.py` (AC: 10)

## Dev Notes

### Key pattern — this is the FIRST Epic 8 story; it establishes the native analyzer pattern

**Bridge.py integration (CRITICAL):** `bridge.py` currently calls bash scripts via `subprocess.run(["bash", script_path], input=file_list, ...)`. Epic 8 ports these to Python callables. The `_ANALYZERS` table (`bridge.py:30-44`) maps tool names to `.sh` script paths. For native ports, the pattern is to replace the script path with a Python callable and update `run_analyzers()` to dispatch accordingly.

**How bridge.py passes files:** `_file_list(cf)` returns `"\n".join(sorted(set(cf.all_files)))` — a newline-joined string. Native analyzers receive `changed_files: ChangedFiles` directly — use `changed_files.shell` (the pre-filtered list of `.sh`/`.bash` paths) rather than re-parsing the string.

**shellcheck -f json1 output format:**
```json
{
  "comments": [
    {
      "file": "test.sh",
      "line": 3,
      "endLine": 3,
      "column": 6,
      "level": "warning",
      "code": 2086,
      "message": "Double quote to prevent globbing and word splitting.",
      "fix": null
    }
  ]
}
```
Note: this is the native `shellcheck -f json1` format (object with `comments` array). The bash wrapper's fixture files at `tests/fixtures/shellcheck/` use this same format. The Python port must parse `data["comments"]` — NOT a flat list.

**Exact Finding construction** (must match bash wrapper output):
```python
Finding(
    severity="High" if item["level"] == "error" else "Medium" if item["level"] == "warning" else "Low",
    confidence=95,
    source="shellcheck",
    file=str(file_path),
    line=item["line"],
    finding=f"SC{item['code']}: {item['message']}",
    remediation=f"See https://www.shellcheck.net/wiki/SC{item['code']}",
)
```

**Only `style` and `info` levels → Low** (the bash wrapper filters to `warning|error` only via `-S warning` flag; `style`/`info` are excluded at the binary level — but if they appear in mock fixtures, map them to Low).

**`ChangedFiles.shell`** already contains only `.sh`/`.bash` files — but still filter to files that exist on disk (the list may include deleted files).

**Logger pattern** from `sarif.py`:
```python
import logging
logger = logging.getLogger(__name__)
logger.warning("[ai-pr-review] WARNING: shellcheck not found; skipping.")
```

**`native/` directory is new** — create `ai_pr_review/analyzers/native/__init__.py` (empty).

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/shellcheck.py`
- New file: `ai_pr_review/analyzers/native/__init__.py`
- Modified: `ai_pr_review/analyzers/bridge.py` — `_ANALYZERS` table + dispatch logic
- New test: `tests/python/test_analyzer_shellcheck.py`
- Fixture files at `tests/fixtures/shellcheck/shellcheck-*.json` — read these for test data

### References

- [Source: ai_pr_review/analyzers/bridge.py#_ANALYZERS] — table to modify at lines 30-44
- [Source: ai_pr_review/analyzers/bridge.py#run_analyzers] — dispatch function to extend
- [Source: ai_pr_review/analyzers/sarif.py] — reference pattern for native analyzer (logging, Finding construction, error handling)
- [Source: ai_pr_review/findings/models.py] — Finding model: `severity`, `confidence`, `finding`, `source`, `file`, `line`, `remediation`
- [Source: ai_pr_review/manifest.py#ChangedFiles] — `changed_files.shell` is the pre-filtered shell file list
- [Source: tests/fixtures/shellcheck/] — fixture files for test data (shellcheck-warning.json, shellcheck-error.json, shellcheck-empty.json, shellcheck-malformed.json)
- [Source: tests/python/test_bridge.py] — existing test patterns to follow
- [Source: analyzers/run-shellcheck.sh] — bash wrapper being replaced (reference only)
- [Source: docs/analyzers-bash-inventory.md] — shellcheck section with parity constants
- GitHub issue: #462

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
