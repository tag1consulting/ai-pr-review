# Story 8.12: Port run-trufflehog.sh to Native Python

Status: ready-for-dev

## Story

As a maintainer,
I want `run-trufflehog.sh` replaced by a native Python function in `bridge.py`,
so that secret scanning is performed without spawning a bash subprocess and the allowlist YAML parser is replaced with PyYAML.

## Acceptance Criteria

1. `_run_trufflehog(changed_files, diff_file)` returns `list[Finding]` — no bash subprocess
2. Dual scan mode: git mode (`trufflehog git file://<workspace> --json --since-commit <BASE_SHA> --branch <HEAD_SHA>`) when both `BASE_SHA` and `HEAD_SHA` env vars set; filesystem mode otherwise
3. Multi-axis severity matrix (exact):
   - `Verified=true` → Critical
   - `Verified=false`, `IsTest=false` → High
   - `Verified=false`, `IsTest=true` OR detector name/ID contains `test`/`sample`/`fake` → Medium
4. Per-finding confidence: `Verified=true` → 95, `Verified=false` → 85
5. `source = "trufflehog"` on all findings
6. Allowlist: read `trufflehog-allowlist.yml` from workspace root using `yaml.safe_load`; suppress findings where both `detector` AND `raw` match
7. NDJSON parsing: handle malformed lines gracefully (skip line, continue)
8. Path normalization: strip workspace prefix from `SourceMetadata.Data.Filesystem.file` or `SourceMetadata.Data.Git.file`
9. Binary-absent path: returns `[]`, logs WARNING, does not raise
10. All fixtures from `tests/fixtures/trufflehog/` migrated to `tests/python/test_analyzer_trufflehog.py`
11. `mypy --strict` and `ruff check` pass

## Tasks / Subtasks

- [ ] Create `ai_pr_review/analyzers/native/trufflehog.py` (AC: 1-9)
  - [ ] Implement `_is_test_path(file_path: str) -> bool` using regex matching test patterns
  - [ ] Implement `_load_allowlist() -> list[dict]` — read `trufflehog-allowlist.yml` with PyYAML
  - [ ] Implement `_is_allowlisted(finding: dict, allowlist: list[dict]) -> bool`
  - [ ] Implement scan-mode selection based on `BASE_SHA`/`HEAD_SHA` env vars
  - [ ] Run trufflehog with `--json` flag; collect NDJSON output
  - [ ] Parse NDJSON: `json.loads(line)` per line in try/except; skip on error
  - [ ] Apply severity matrix and confidence assignment
  - [ ] Apply allowlist suppression
  - [ ] Strip workspace prefix from file paths in `SourceMetadata`
- [ ] Update `_ANALYZERS` table in `bridge.py`
- [ ] Create `tests/python/test_analyzer_trufflehog.py` (AC: 10)
  - [ ] Test: verified finding → Critical/confidence=95
  - [ ] Test: unverified non-test → High/confidence=85
  - [ ] Test: unverified test-file path → Medium
  - [ ] Test: unverified test-pattern detector name → Medium
  - [ ] Test: allowlist suppression (both fields match → suppressed)
  - [ ] Test: allowlist partial match (only one field → not suppressed)
  - [ ] Test: malformed NDJSON line skipped
  - [ ] Test: empty output → `[]`
  - [ ] Test: binary absent → `[]`
- [ ] Run mypy + ruff check (AC: 11)

## Dev Notes

### Trufflehog NDJSON output format (`trufflehog ... --json`)

Each line is a separate JSON object (NDJSON, NOT a JSON array):
```json
{"DetectorName":"AWS","Verified":true,"Raw":"AKIAIOSFODNN7EXAMPLE","SourceMetadata":{"Data":{"Filesystem":{"file":"config/settings.py","line":15}}}}
{"DetectorName":"GitHub","Verified":false,"Raw":"ghp_xxx","SourceMetadata":{"Data":{"Filesystem":{"file":"scripts/deploy.sh","line":7}}}}
```

Parse line by line:
```python
for line in output.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue  # skip malformed line
```

**Exact severity matrix:**
```python
verified = item.get("Verified", False)
detector_name = item.get("DetectorName", "").lower()
detector_type = str(item.get("DetectorType", "")).lower()
file_path = _extract_file_path(item)
is_test = _is_test_path(file_path) or any(
    kw in detector_name for kw in ("test", "sample", "fake")
) or any(
    kw in detector_type for kw in ("test", "sample", "fake")
)

if verified:
    severity, confidence = "Critical", 95
elif not is_test:
    severity, confidence = "High", 85
else:
    severity, confidence = "Medium", 85
```

**File path extraction from SourceMetadata:**
```python
def _extract_file_path(item: dict) -> str:
    meta = item.get("SourceMetadata", {}).get("Data", {})
    # Filesystem mode
    if "Filesystem" in meta:
        return meta["Filesystem"].get("file", "")
    # Git mode
    if "Git" in meta:
        return meta["Git"].get("file", "")
    return ""
```

**Path stripping** (workspace prefix):
```python
workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
if file_path.startswith(workspace):
    file_path = file_path[len(workspace):].lstrip("/")
```

**Line extraction:**
```python
def _extract_line(item: dict) -> int | None:
    meta = item.get("SourceMetadata", {}).get("Data", {})
    for key in ("Filesystem", "Git"):
        if key in meta:
            ln = meta[key].get("line")
            return int(ln) if ln and int(ln) >= 1 else None
    return None
```

**Allowlist format** (`trufflehog-allowlist.yml`):
```yaml
allowlist:
  paths:
    - "tests/fixtures/"
  detectors:
    - detector: "AWS"
      raw: "AKIAIOSFODNN7EXAMPLE"
```
Load with `yaml.safe_load`. The allowlist suppression in the bash wrapper matches on `detector` + `raw` — BOTH must match for suppression.

**PyYAML is already a project dependency** (it's in `requirements*.txt` for other tooling). Verify with `grep -r PyYAML requirements`.

**Test path pattern** (from bash wrapper):
```python
import re
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|fixtures?|testdata|test_data|mocks?|stubs?|fakes?|examples?|samples?)/|"
    r"_test\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$|\.bats$|^test_[^/]+\.[a-z]+$|(^|/)test_[^/]+\.[a-z]+$"
)
def _is_test_path(file_path: str) -> bool:
    return bool(_TEST_PATH_RE.search(file_path))
```

**Dual scan mode:**
```python
base_sha = os.environ.get("BASE_SHA")
head_sha = os.environ.get("HEAD_SHA")
workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
if base_sha and head_sha:
    cmd = ["trufflehog", "git", f"file://{workspace}", "--json",
           f"--since-commit={base_sha}", f"--branch={head_sha}"]
else:
    # Filesystem mode — scan each changed file
    cmd = ["trufflehog", "filesystem", "--json"] + list(changed_files.all_files)
```

**Finding text:** Use `DetectorName` and `Raw` (truncated) as the finding text:
```python
raw = (item.get("Raw") or "")[:50]
finding_text = f"{item.get('DetectorName', 'unknown')}: potential secret detected (raw: {raw}...)"
```

### Key risk: Allowlist suppression parity

The bash wrapper's awk-based YAML parser is fragile — the PyYAML replacement must match the same suppression semantics: BOTH `detector` and `raw` must match. A partial match (only one field) must NOT suppress. This is the highest-risk parity requirement in this story.

### Project Structure Notes

- New file: `ai_pr_review/analyzers/native/trufflehog.py`
- Modified: `ai_pr_review/analyzers/bridge.py`
- New test: `tests/python/test_analyzer_trufflehog.py`
- Fixtures: `tests/fixtures/trufflehog/` — verified, unverified, test-file, allowlisted, empty variants

### References

- [Source: ai_pr_review/analyzers/bridge.py:30-44] — `_ANALYZERS` table; trufflehog gated on `[]` (always runs)
- [Source: ai_pr_review/findings/models.py] — Finding model; note `severity: Literal["Critical","High","Medium","Low"]`
- [Source: tests/fixtures/trufflehog/] — trufflehog-verified.json, trufflehog-unverified.json, trufflehog-unverified-test-file.json, trufflehog-allowlisted-path.json, trufflehog-empty.json
- [Source: analyzers/run-trufflehog.sh] — bash wrapper (reference for exact allowlist and severity logic)
- GitHub issue: #473

## Dev Agent Record

### Agent Model Used

_to be filled_

### Debug Log References

### Completion Notes List

### File List
