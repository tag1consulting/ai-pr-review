# Story 8.4: Port run-kube-linter.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-kube-linter.sh` replaced by a native Python function in `bridge.py`,
so that Kubernetes manifest linting is performed without spawning a bash subprocess.

## Acceptance Criteria

1. `_run_kube_linter(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Eligibility pre-sniff: only files containing both `apiVersion:` and `kind:` are passed
3. Binary invocation: `kube-linter lint --format json <eligible-files...>`
4. All findings → Medium severity
5. `confidence = 85` on all findings
6. `source = "kube-linter"` on all findings
7. `line = None` (kube-linter does not emit line numbers; do NOT set `line=0` — use `None`)
8. Path normalization: none
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. Fixtures from `tests/fixtures/kubelinter/` migrated to `tests/python/test_analyzer_kube_linter.py`
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/kube_linter.py` (AC: 1-9)
  - [ ] Gather candidates from `changed_files.iac` + any `.yaml`/`.yml`/`.json` in `changed_files.all_files`
  - [ ] Content-sniff: read first 50 lines of each candidate; keep only if `apiVersion:` AND `kind:` both present
  - [ ] If no eligible files, return `[]`
  - [ ] Check `shutil.which("kube-linter")`; return `[]` + log WARNING if absent
  - [ ] Run `kube-linter lint --format json <eligible-files>` via subprocess
  - [ ] Parse `Reports[].Object.Metadata.FilePath`, `Reports[].Diagnostic.Message`, `Reports[].Check`
  - [ ] All findings → Medium; confidence = 85; line = None
- [ ] Update `_ANALYZERS` table in `bridge.py`: replace `"run-kube-linter.sh"` entry
- [ ] Create `tests/python/test_analyzer_kube_linter.py` (AC: 10)
  - [ ] Test: violations fixture → Medium/confidence=85/source="kube-linter"/line=None
  - [ ] Test: empty fixture → `[]`
  - [ ] Test: malformed fixture → `[]`
  - [ ] Test: content-sniff excludes non-k8s YAML files
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### Kube-linter JSON output format (`kube-linter lint --format json`)

```json
{
  "Reports": [
    {
      "Object": {
        "Metadata": {
          "FilePath": "k8s/deployment.yaml",
          "Name": "my-app",
          "Namespace": "default"
        },
        "K8sObject": {"GroupVersionKind": {"Group": "apps", "Version": "v1", "Kind": "Deployment"}}
      },
      "Diagnostic": {
        "Message": "container \"app\" does not have a read-only root file system"
      },
      "Check": "no-read-only-root-fs",
      "Remediation": "Set readOnlyRootFilesystem to true..."
    }
  ],
  "Summary": {"ChecksStatus": "FAIL"}
}
```
Note: root object (not flat array). Access `data["Reports"]`.

**Exact Finding construction:**
```python
for report in data.get("Reports") or []:
    obj = report.get("Object", {})
    meta = obj.get("Metadata", {})
    file_path = meta.get("FilePath", "")
    message = report.get("Diagnostic", {}).get("Message", "")
    check = report.get("Check", "")
    remediation = report.get("Remediation", "")
    Finding(
        severity="Medium",
        confidence=85,
        source="kube-linter",
        file=file_path,
        line=None,   # kube-linter has no line numbers
        finding=f"{check}: {message}" if check else message,
        remediation=remediation,
    )
```

**`line=None` not `line=0`:** The `Finding` model has `line: int | None = Field(default=None, ge=1)`. Setting `line=0` would fail pydantic validation. Use `line=None`.

**Content-sniff logic:**
```python
def _is_k8s_manifest(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = "".join(f.readline() for _ in range(50))
        return "apiVersion:" in head and "kind:" in head
    except OSError:
        return False
```

**File gathering:** `changed_files.iac` is the primary source (k8s YAML heuristic from manifest.py). Also include `.yaml`/`.yml` files from `changed_files.config` that pass the content sniff. Do not rely solely on `changed_files.iac` — it uses a path-pattern heuristic that may miss flat YAML files.

**`native/` directory:** Created by Story 8-1. Ensure `__init__.py` exists.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/kube_linter.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_kube_linter.py`
- Fixtures: `tests/fixtures/kubelinter/kubelinter-violations.json`, `kubelinter-empty.json`, `kubelinter-malformed.json`

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table
- [Source: ai_pr_review/findings/models.py:22] — `line: int | None = Field(default=None, ge=1)`
- [Source: ai_pr_review/manifest.py#ChangedFiles.iac] — IaC file list
- [Source: tests/fixtures/kubelinter/] — fixture files
- [Source: analyzers/run-kube-linter.sh] — bash wrapper (reference)
- GitHub issue: #465

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
