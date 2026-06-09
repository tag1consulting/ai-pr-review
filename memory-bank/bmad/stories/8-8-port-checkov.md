# Story 8.8 — Port run-checkov.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-8
**Story Key:** 8-8-port-checkov
**GitHub Issue:** #469
**Status:** ready-for-dev
**Batch:** 3 (MED)
**Gated on:** 8-1 (pattern established)

---

## User Story

As a **maintainer**, I want `run-checkov.sh` replaced by a native Python function in `bridge.py`, so that IaC security findings are produced without spawning a bash subprocess and object/array output normalization is handled in Python.

---

## Acceptance Criteria

- [ ] `analyzers/run-checkov.sh` subprocess call replaced by `_run_checkov(changed_files, diff_file)` returning `list[Finding]`
- [ ] IaC eligibility sniff: only invoke if changed files contain `.tf`/`.tfvars`, Kubernetes YAML (with `apiVersion:`+`kind:`), `Dockerfile*`, or CloudFormation JSON
- [ ] Binary invocation: `checkov -d <workspace> --output json --compact`
- [ ] Output normalization: handle both single-object and array-of-objects checkov output
- [ ] Severity map: check ID prefix `CKV2_*` or `CKV_SECRET_*` → High; all others → Medium
- [ ] `confidence = 80` on all findings
- [ ] `source = "checkov"` on all findings
- [ ] Path normalization: strip leading `/` from `repo_file_path`
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/checkov/` migrated to `tests/python/test_analyzer_checkov.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_checkov(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/checkov.py`
2. IaC eligibility sniff: grep changed files for IaC indicators
3. Run `checkov -d <workspace> --output json --compact` via `subprocess.run`
4. Normalize output: if root is a dict, wrap in list; if already a list, use as-is
5. For each `failed_checks` item: map `check_id`, `repo_file_path`, `file_line_range`, `guideline`
6. Severity: `CKV2_` or `CKV_SECRET_` prefix → High; else → Medium
7. Strip leading `/` from paths
8. Update `_ANALYZERS` table in `bridge.py`
9. Create `tests/python/test_analyzer_checkov.py`

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"checkov"` |
| `confidence` | 80 |
| Severity default | Medium |
| Path normalization | Strip leading `/` from `repo_file_path` |

---

## References

- Epic issue: #461
- GitHub story: #469
- Parity reference: `docs/analyzers-bash-inventory.md` — checkov section
