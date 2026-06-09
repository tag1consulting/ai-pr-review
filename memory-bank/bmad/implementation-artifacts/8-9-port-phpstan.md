# Story 8.9: Port run-phpstan.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-phpstan.sh` replaced by a native Python function in `bridge.py`,
so that PHP static analysis findings are produced without spawning a bash subprocess and config/autoload discovery is handled in Python.

## Acceptance Criteria

1. `_run_phpstan(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Config discovery: check for `phpstan.neon` or `phpstan.neon.dist` in workspace root
3. Autoload: add `--autoload-file=vendor/autoload.php` if `vendor/autoload.php` exists
4. Level: honor `PHPSTAN_LEVEL` env var (default 3); overridden by config file if one found
5. Binary invocation: `phpstan analyse --error-format=json --level=<N> [--configuration=<path>] [--autoload-file=...] <files...>`
6. All findings → High severity
7. `confidence = 85` on all findings
8. `source = "phpstan"` on all findings
9. Path normalization: strip `GITHUB_WORKSPACE` or `PWD` prefix from absolute paths
10. File filter: `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`
11. Binary-absent path: returns `[]`, logs WARNING, does not raise
12. Fixtures from `tests/fixtures/phpstan/` migrated to `tests/python/test_analyzer_phpstan.py`
13. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/phpstan.py` (AC: 1-11)
  - [ ] Filter `changed_files.all_files` to PHP extensions
  - [ ] Check `shutil.which("phpstan")`; return `[]` + log WARNING if absent
  - [ ] Config discovery: `Path("phpstan.neon").exists()` or `Path("phpstan.neon.dist").exists()`
  - [ ] Autoload: check `Path("vendor/autoload.php").exists()`
  - [ ] Level: `int(os.environ.get("PHPSTAN_LEVEL", "3"))`; skip `--level` if config found
  - [ ] Build and run phpstan invocation
  - [ ] Parse JSON output: `data["files"]` dict → `errors[]` → `line`, `message`
  - [ ] All findings → High; confidence = 85
  - [ ] Strip workspace prefix from absolute paths
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_phpstan.py` (AC: 12)
  - [ ] Test: findings fixture → High/confidence=85/source="phpstan"
  - [ ] Test: empty → `[]`
  - [ ] Test: PHPSTAN_LEVEL env var honored
  - [ ] Test: config file discovery (mock path existence)
  - [ ] Test: autoload discovery
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 13)

## Dev Notes

### phpstan JSON output format (`phpstan analyse --error-format=json`)

```json
{
  "totals": {"errors": 1, "file_errors": 1},
  "files": {
    "/workspace/src/Service.php": {
      "errors": 1,
      "messages": [
        {
          "message": "Parameter $id of method Service::find() has no type hint.",
          "line": 42,
          "ignorable": true
        }
      ]
    }
  },
  "errors": []
}
```
Same structure as phpcs: `files` dict keyed by absolute path, each with `messages[]` containing `line` and `message`.

**Exact Finding construction:**
```python
for abs_path, file_data in data.get("files", {}).items():
    rel_path = _strip_workspace(abs_path)
    for msg in file_data.get("messages", []):
        Finding(
            severity="High",  # phpstan has no severity levels
            confidence=85,
            source="phpstan",
            file=rel_path,
            line=msg.get("line"),
            finding=msg.get("message", ""),
        )
```

**Build invocation:**
```python
cmd = ["phpstan", "analyse", "--error-format=json"]
config_path = None
for name in ("phpstan.neon", "phpstan.neon.dist"):
    if Path(name).exists():
        config_path = name
        break
if config_path:
    cmd += [f"--configuration={config_path}"]
else:
    level = os.environ.get("PHPSTAN_LEVEL", "3")
    cmd += [f"--level={level}"]
if Path("vendor/autoload.php").exists():
    cmd += ["--autoload-file=vendor/autoload.php"]
cmd += php_files
```

**Level when config found:** When a phpstan.neon config is present, phpstan ignores `--level` (the config sets it). Do not pass `--level` if a config file was found — some phpstan versions error on conflicting values.

**All findings are High:** phpstan has no severity levels — every finding is High. This is correct and intentional (it's a strict static analyzer).

**PHP extensions:** Same as phpcs — `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`. Use `changed_files.all_files` filtered by suffix, not just `changed_files.php`.

**Exit codes:** phpstan exits 1 when issues found, 0 when clean. Both valid.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/phpstan.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_phpstan.py`
- Fixtures: `tests/fixtures/phpstan/`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: tests/fixtures/phpstan/] — fixture files
- [Source: analyzers/run-phpstan.sh] — bash wrapper (reference)
- GitHub issue: #470

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
