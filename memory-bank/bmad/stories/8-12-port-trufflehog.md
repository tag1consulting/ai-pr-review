# Story 8.12 — Port run-trufflehog.sh to Native Python

**Epic:** 8 — Port Analyzer Wrappers to Native Python
**Story ID:** 8-12
**Story Key:** 8-12-port-trufflehog
**GitHub Issue:** #473
**Status:** ready-for-dev
**Batch:** 4 (HIGH)
**Gated on:** 8-1 through 8-11 (all MED stories complete; pattern fully established)

---

## User Story

As a **maintainer**, I want `run-trufflehog.sh` replaced by a native Python function in `bridge.py`, so that secret scanning is performed without spawning a bash subprocess and the allowlist YAML parser is replaced with PyYAML.

---

## Acceptance Criteria

- [ ] `analyzers/run-trufflehog.sh` subprocess call replaced by `_run_trufflehog(changed_files, diff_file)` returning `list[Finding]`
- [ ] Dual scan-mode: git mode (`trufflehog git file://<workspace> --json --since-commit <base> --branch <head>`) when `BASE_SHA`+`HEAD_SHA` env vars set; filesystem mode otherwise
- [ ] Multi-axis severity matrix (exact):
  - `Verified=true` → Critical
  - `Verified=false`, `IsTest=false` → High
  - `Verified=false`, `IsTest=true` OR detector name/ID contains `test`/`sample`/`fake` → Medium
- [ ] Per-finding confidence: `Verified=true` → 95, `Verified=false` → 85
- [ ] `source = "trufflehog"` on all findings
- [ ] Allowlist: read `trufflehog-allowlist.yml` from workspace root using `yaml.safe_load` (PyYAML); suppress findings matching `detector` + `raw`
- [ ] NDJSON parsing: handle malformed lines gracefully (skip line, continue)
- [ ] Path normalization: strip workspace prefix from `SourceMetadata.Data.Filesystem.file` or `SourceMetadata.Data.Git.file`
- [ ] Binary-absent path: returns empty list, logs WARNING, does not raise
- [ ] All fixtures from `tests/fixtures/trufflehog/` migrated to `tests/python/test_analyzer_trufflehog.py`
- [ ] `mypy --strict` and `ruff check` pass

---

## Implementation Tasks

1. Add `_run_trufflehog(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]` to `ai_pr_review/analyzers/native/trufflehog.py`
2. Scan-mode selection: check `os.environ.get("BASE_SHA")` and `os.environ.get("HEAD_SHA")`
3. Invoke trufflehog with appropriate flags; capture NDJSON output line by line
4. For each line: `json.loads(line)` in try/except; skip on error
5. Implement multi-axis severity logic as an explicit decision table
6. Per-finding confidence assignment (95/85)
7. Allowlist: `yaml.safe_load(Path("trufflehog-allowlist.yml").read_text())` if file exists; suppress matching findings
8. Path normalization from `SourceMetadata` fields
9. Update `_ANALYZERS` table in `bridge.py`
10. Create `tests/python/test_analyzer_trufflehog.py`; ensure PyYAML allowlist is tested

---

## Parity Constants

| Field | Value |
|-------|-------|
| `source` | `"trufflehog"` |
| `confidence` | 95 (verified) or 85 (unverified) — per finding |
| Severity default | N/A (always explicit via matrix) |
| Path normalization | Strip workspace prefix from SourceMetadata path |

---

## Key risk

The awk-based YAML allowlist parser in the bash wrapper is the highest-risk piece. The PyYAML replacement must reproduce the suppression semantics exactly: both `detector` and `raw` fields must match for suppression to apply.

---

## References

- Epic issue: #461
- GitHub story: #473
- Parity reference: `docs/analyzers-bash-inventory.md` — trufflehog section
