# Story 8.8: Port run-checkov.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-checkov.sh` replaced by a native Python function in `bridge.py`,
so that IaC security findings are produced without spawning a bash subprocess and object/array output normalization is handled in Python.

## Acceptance Criteria

1. `_run_checkov(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. IaC eligibility sniff: only invoke if changed files include `.tf`/`.tfvars`, K8s YAML (`apiVersion:`+`kind:`), `Dockerfile*`, or CloudFormation JSON
3. Binary invocation: `checkov -d <workspace> --output json --compact`
4. Output normalization: handle both single-object and array-of-objects checkov output
5. Severity: `CKV2_*` or `CKV_SECRET_*` prefix → High; all others → Medium
6. `confidence = 80` on all findings
7. `source = "checkov"` on all findings
8. Path normalization: strip leading `/` from `repo_file_path`
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. Fixtures from `tests/fixtures/checkov/` migrated to `tests/python/test_analyzer_checkov.py`
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/checkov.py` (AC: 1-9)
  - [ ] IaC eligibility check across `changed_files` categories
  - [ ] Check `shutil.which("checkov")`; return `[]` + log WARNING if absent
  - [ ] Run `checkov -d <workspace> --output json --compact` where workspace = `os.getcwd()`
  - [ ] Normalize output: `isinstance(data, dict)` → wrap in `[data]`; already list → use as-is
  - [ ] For each framework result in the list: extract `failed_checks` array
  - [ ] Map `check_id`, `repo_file_path`, `file_line_range[0]`, `guideline` to Finding
  - [ ] Severity: `check_id.startswith("CKV2_") or check_id.startswith("CKV_SECRET_")` → High, else → Medium
  - [ ] Strip leading `/` from `repo_file_path`
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_checkov.py` (AC: 10)
  - [ ] Test: CKV2_ prefix → High
  - [ ] Test: CKV_SECRET_ prefix → High
  - [ ] Test: regular CKV_ → Medium
  - [ ] Test: single-object output normalized to list
  - [ ] Test: array output used directly
  - [ ] Test: path stripping (leading `/` removed)
  - [ ] Test: binary absent → `[]`
  - [ ] Test: IaC eligibility check (no IaC files → `[]`)
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### Checkov JSON output format (`checkov -d . --output json --compact`)

Checkov can return EITHER a single object (one framework) or an array of objects (multiple frameworks). This is the key normalization challenge.

**Single-framework output:**
```json
{
  "check_type": "terraform",
  "results": {
    "passed_checks": [],
    "failed_checks": [
      {
        "check_id": "CKV_AWS_8",
        "check": "Ensure that instances are not publicly exposed to the internet",
        "check_result": {"result": "FAILED"},
        "code_block": [[1, "resource..."]],
        "file_path": "/workspace/main.tf",
        "repo_file_path": "/main.tf",
        "file_line_range": [1, 20],
        "resource": "aws_instance.example",
        "guideline": "https://docs.bridgecrew.io/docs/..."
      }
    ]
  }
}
```

**Multi-framework output:** array of the above objects.

**Output normalization:**
```python
if isinstance(data, dict):
    data = [data]
# data is now always a list
```

**Iterating failed checks:**
```python
for framework_result in data:
    if not isinstance(framework_result, dict):
        continue
    results = framework_result.get("results", {})
    for check in results.get("failed_checks", []):
        check_id = check.get("check_id", "")
        severity = "High" if check_id.startswith(("CKV2_", "CKV_SECRET_")) else "Medium"
        repo_path = check.get("repo_file_path", "").lstrip("/")
        line_range = check.get("file_line_range", [None])
        Finding(
            severity=severity,
            confidence=80,
            source="checkov",
            file=repo_path,
            line=line_range[0] if line_range else None,
            finding=f"{check_id}: {check.get('check', '')}",
            remediation=check.get("guideline", ""),
        )
```

**IaC eligibility:**
```python
def _has_iac_files(cf: ChangedFiles) -> bool:
    if cf.terraform or cf.dockerfile:
        return True
    # K8s YAML: any yaml in changed files with apiVersion+kind
    for f in cf.all_files:
        if f.endswith((".yaml", ".yml")) and _is_k8s_manifest(f):
            return True
    # CloudFormation: JSON with Resources key
    for f in cf.all_files:
        if f.endswith(".json") and _is_cfn(f):
            return True
    return False
```

**`-d <workspace>` not `-f <file>`:** Checkov runs against a directory (`-d`) and scans everything it finds. Pass `os.getcwd()` as the workspace. The output's `repo_file_path` is relative to that directory (with a leading `/` to strip).

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/checkov.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_checkov.py`
- Fixtures: `tests/fixtures/checkov/` — check fixture files

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/findings/models.py] — Finding model
- [Source: ai_pr_review/manifest.py#ChangedFiles] — terraform, dockerfile, iac fields
- [Source: tests/fixtures/checkov/] — fixture files
- [Source: analyzers/run-checkov.sh] — bash wrapper (reference)
- GitHub issue: #469

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
