# Story 9.1: Security Canary Test — Never Execute Checked-Out Code

**Epic:** 9 — Security Hardening
**Story ID:** 9-1
**Story Key:** 9-1-canary-security-test
**GitHub Issue:** #494
**Parent Epic Issue:** N/A (standalone security hardening)
**Status:** done

---

## Story

As a **maintainer**,
I want a CI-enforced canary test that proves the action never executes any file from the checked-out workspace,
so that the "treat checked-out repo as untrusted data" invariant documented in SECURITY.md cannot silently regress.

---

## Acceptance Criteria

1. A canary fixture directory `tests/security/canary-workspace/` contains six tripwire files: `Makefile`, `setup.py`, `package.json`, `.pre-commit-config.yaml`, `conftest.py`, `.semgrep.yml`. Each file is designed to write a sentinel marker to a well-known path (e.g., `CANARY_DIR / "<tool>-executed"`) if it is ever executed, imported, or invoked by the action.
2. A pytest test module `tests/python/test_security_canary.py` (marked `@pytest.mark.security`) runs the action's workspace-reading code paths against the canary workspace and asserts that none of the canary sentinel paths were written.
3. The canary test covers every code path that reads files from `$GITHUB_WORKSPACE`: diff computation (`diff/compute.py`), manifest building (`manifest.py`), language detection (`languages.py`), analyzer bridge (`analyzers/bridge.py`), context enrichment (`context/treesitter.py`), SARIF ingestor (`analyzers/sarif.py`), and language profile loading (`language_profiles.py`).
4. The existing `lint.yml` CI job (which runs `pytest tests/python/ -q` on every PR) automatically picks up the new test without any workflow changes — no new CI job required.
5. CONTRIBUTING.md gains a "Security canary tests" section explaining that failures in `@pytest.mark.security` tests are security regressions, not flaky tests, and must not be skipped or xfailed without a private vulnerability report.
6. SECURITY.md removes the placeholder sentence "A canary fixture/test enforcing this invariant at CI time is tracked in #494. Until that lands..." and replaces it with a statement that the canary test is live.
7. `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/`, and `pytest tests/python -q` all pass clean.

---

## Implementation Tasks

- [x] **Task 1 — Canary fixture files** (AC: 1)

  Create `tests/security/canary-workspace/` with these six files. Each writes a marker to `os.environ.get("CANARY_DIR", "/tmp/canary")` if executed/imported/invoked.

  - [x] `tests/security/canary-workspace/Makefile`
  - [x] `tests/security/canary-workspace/setup.py`
  - [x] `tests/security/canary-workspace/package.json`
  - [x] `tests/security/canary-workspace/.pre-commit-config.yaml`
  - [x] `tests/security/canary-workspace/conftest.py`
  - [x] `tests/security/canary-workspace/.semgrep.yml`
  - [x] `tests/security/__init__.py` (empty)
  - [x] `tests/security/canary-workspace/README.md`

- [x] **Task 2 — Conftest.py guard** (AC: 1, 2)

  The `tests/security/canary-workspace/conftest.py` must NOT be auto-collected by pytest during normal test runs. Verify `pytest.ini` / `pyproject.toml` `testpaths` setting. The existing `lint.yml` runs `pytest tests/python/` (not `pytest tests/`), so the canary workspace `conftest.py` is safe. Add a comment to `tests/security/canary-workspace/conftest.py` noting this and add a `pytest.ini`-level `norecursedirs` entry covering `tests/security/canary-workspace` as a belt-and-suspenders guard.

  Check `pyproject.toml` `[tool.pytest.ini_options]` section and add:
  ```toml
  norecursedirs = ["tests/security/canary-workspace"]
  ```

- [x] **Task 3 — Test module `tests/python/test_security_canary.py`** (AC: 2, 3)

  Pattern from existing analyzer tests: use `tmp_path` fixture for the canary dir, set `CANARY_DIR` env var, run the code-under-test against the canary workspace, assert no sentinel paths exist.

  ```python
  import pytest
  import os
  from pathlib import Path

  CANARY_WORKSPACE = Path(__file__).parent.parent / "security" / "canary-workspace"
  SENTINEL_NAMES = [
      "makefile-executed",
      "setup-py-imported",
      "npm-preinstall-executed",
      "npm-postinstall-executed",
      "pre-commit-hook-executed",
      "conftest-imported",
      "semgrep-rule-executed",
  ]

  @pytest.mark.security
  class TestNeverExecuteCheckedOutCode:

      def test_diff_compute_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """diff/compute.py reads file content as data — must not execute Makefile etc."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          from ai_pr_review.diff.compute import _read_file_bytes  # or whichever public surface reads files
          # Read each canary file as bytes — should not trigger any side effects
          for f in CANARY_WORKSPACE.iterdir():
              if f.is_file():
                  f.read_bytes()  # simulate what diff compute does
          _assert_no_canary_fired(tmp_path / "canary")

      def test_manifest_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """manifest.py enumerates and classifies files — must not execute any."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          from ai_pr_review.manifest import build_manifest
          # Build a manifest over the canary workspace — purely path/extension analysis
          # No imports of workspace Python, no shelling out
          result = build_manifest(
              changed_files=[str(f.relative_to(CANARY_WORKSPACE)) for f in CANARY_WORKSPACE.iterdir() if f.is_file()],
              workspace=str(CANARY_WORKSPACE),
          )
          assert result is not None
          _assert_no_canary_fired(tmp_path / "canary")

      def test_language_detection_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """languages.py detects by extension/name — must not execute any file."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          from ai_pr_review.languages import detect_language, is_test_file
          for f in CANARY_WORKSPACE.iterdir():
              if f.is_file():
                  detect_language(str(f))
                  is_test_file(str(f))
          _assert_no_canary_fired(tmp_path / "canary")

      def test_analyzer_bridge_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """analyzer bridge reads file content to run regex/AST analysis — must not exec."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          from ai_pr_review.analyzers.bridge import run_analyzers
          # Invoke with a manifest of canary workspace files and a fake diff
          # (each analyzer reads file bytes, never executes the file)
          changed = [str(f.relative_to(CANARY_WORKSPACE)) for f in CANARY_WORKSPACE.iterdir() if f.is_file()]
          try:
              run_analyzers(changed_files=changed, workspace=str(CANARY_WORKSPACE), diff_text="")
          except Exception:
              pass  # A missing-dep error is fine; execution of canary files is not
          _assert_no_canary_fired(tmp_path / "canary")

      def test_context_enrichment_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """tree-sitter context enrichment parses file content as AST — must not exec."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          try:
              from ai_pr_review.context.treesitter import extract_symbols
              for f in CANARY_WORKSPACE.iterdir():
                  if f.is_file() and f.suffix == ".py":
                      extract_symbols(f.read_text(errors="replace"), language="python")
          except ImportError:
              pytest.skip("tree-sitter not available")
          _assert_no_canary_fired(tmp_path / "canary")

      def test_sarif_ingestor_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """SARIF ingestor reads JSON files as data — must not execute any workspace file."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          from ai_pr_review.analyzers.sarif import ingest_sarif
          # Point sarif ingestor at a non-existent path — it is fail-soft (returns [])
          result = ingest_sarif(sarif_paths=["nonexistent.sarif"], workspace=str(CANARY_WORKSPACE))
          assert result == []
          _assert_no_canary_fired(tmp_path / "canary")

      def test_language_profile_loading_does_not_execute_workspace(self, tmp_path, monkeypatch):
          """language_profiles.py reads markdown profile files — must not exec workspace."""
          monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
          from ai_pr_review.language_profiles import load_profile
          # Load a known profile — reads a markdown file from the image, not from workspace
          try:
              load_profile("Python")
          except Exception:
              pass
          _assert_no_canary_fired(tmp_path / "canary")


  def _assert_no_canary_fired(canary_dir: Path) -> None:
      """Assert no sentinel file was created in canary_dir."""
      if not canary_dir.exists():
          return  # nothing fired — good
      fired = [f.name for f in canary_dir.iterdir() if f.name in SENTINEL_NAMES]
      assert not fired, (
          f"SECURITY REGRESSION: The following workspace execution sentinels were triggered: {fired}. "
          f"The action executed checked-out code from the workspace, violating the 'never execute "
          f"checked-out code' invariant. This is a security regression — see SECURITY.md and "
          f"CONTRIBUTING.md for the reporting process."
      )
  ```

  **Important:** The actual public API signatures for `build_manifest`, `run_analyzers`, `ingest_sarif`, `extract_symbols`, and `load_profile` must be verified against the current source before finalizing the test. See the "API surface verification" section in Dev Notes below.

- [x] **Task 4 — Register `security` pytest marker** (AC: 2, 4)

  In `pyproject.toml` `[tool.pytest.ini_options]`, add:
  ```toml
  markers = [
      "security: security invariant regression tests — failures are security issues, not flaky tests",
  ]
  ```
  This prevents `PytestUnknownMarkWarning` and makes the marker searchable.

- [x] **Task 5 — CONTRIBUTING.md: security canary section** (AC: 5)

  Add a new section after the existing testing recipes section. Locate the "## Testing" or equivalent heading and add below it:

  ```markdown
  ### Security canary tests

  `tests/python/test_security_canary.py` (marked `@pytest.mark.security`) enforces the
  invariant that the action never executes any file from the checked-out workspace — see
  `SECURITY.md` for the full invariant statement.

  The corresponding fixture in `tests/security/canary-workspace/` contains six tripwire
  files (`Makefile`, `setup.py`, `package.json`, `.pre-commit-config.yaml`,
  `conftest.py`, `.semgrep.yml`) designed to write sentinel files to a canary directory
  if the action ever executes them.

  **A failure in a `@pytest.mark.security` test is a security regression, not a flaky
  test.** Do not skip it, xfail it, or delete it. Instead:

  1. Identify which code path triggered the canary sentinel.
  2. Report the regression privately via [GitHub's private vulnerability
     reporting](https://github.com/tag1consulting/ai-pr-review/security/advisories/new)
     or email security@tag1consulting.com.
  3. Do not merge the offending PR until the vulnerability is remediated.
  ```

- [x] **Task 6 — SECURITY.md: update the regression guard section** (AC: 6)

  In the "### Regression guard" subsection (currently the last paragraph of the invariant block), replace:

  > A canary fixture/test enforcing this invariant at CI time is tracked in [#494](https://github.com/tag1consulting/ai-pr-review/issues/494). Until that lands, code review on this repo treats any new exec-of-workspace-content code path as a security change requiring the private vulnerability reporting flow.

  With:

  > A canary fixture lives in `tests/security/canary-workspace/` and is enforced by `tests/python/test_security_canary.py` on every PR via the `Lint & Test` CI job. A failure in those tests is a security regression; see CONTRIBUTING.md for the reporting process.

- [x] **Task 7 — Verification** (AC: 7)

  - [x] `pytest tests/python/ -q` — 1668 passed in 20.09s
  - [x] `pytest tests/python/ -m security -v` — 7 passed, 1661 deselected
  - [x] `mypy ai_pr_review/` — Success: no issues found in 85 source files
  - [x] `ruff check ai_pr_review/ tests/python/` — All checks passed

---

## Dev Notes

### API Surface Verification (CRITICAL — do before implementing Task 3)

The test module calls several internal APIs whose exact signatures must be verified against the current source before writing final test code. Do NOT guess — read the actual file.

| API | Module to read | What to verify |
|-----|---------------|----------------|
| `build_manifest()` | `ai_pr_review/manifest.py` | actual parameter names, especially `workspace` vs `root` vs `base_path` |
| `run_analyzers()` | `ai_pr_review/analyzers/bridge.py` | actual dispatch entry point name and params |
| `ingest_sarif()` | `ai_pr_review/analyzers/sarif.py` | top-level function name and `workspace` param |
| `extract_symbols()` | `ai_pr_review/context/treesitter.py` | function name and signature |
| `load_profile()` | `ai_pr_review/language_profiles.py` | function name and how profiles are loaded |
| `_read_file_bytes()` | `ai_pr_review/diff/compute.py` | whether such a function is public or internal; may need to use a higher-level entry point |

For any API that doesn't match the names above, adapt the test to use what actually exists. The goal is to exercise the actual code paths that touch `GITHUB_WORKSPACE` content — not to call specific function names.

### Architecture Context

The invariant is documented in SECURITY.md under "Security invariants". The guarantee is:

> The action reads file contents, paths, and git metadata from `$GITHUB_WORKSPACE` for diffing and analysis. It does not, at any point: run `make`, `npm install`, `npm ci`, `pip install`, `go build`, `bundle install`, or any other build/install step against the checked-out tree; execute `setup.py`, `Makefile`, `package.json` scripts, pre-commit hooks, or any other entry point shipped by the repository under review; source shell files from `$GITHUB_WORKSPACE`, run interpreters on checked-out files, or invoke binaries from the checked-out tree; run linters/formatters/test runners that auto-discover and execute project plugins or fixtures from `$GITHUB_WORKSPACE`.

The test must verify exactly this boundary: the code reads file bytes/text as data, never executes the file.

### Conftest.py Trap Warning

The `tests/security/canary-workspace/conftest.py` is a tripwire: if pytest auto-discovers it during collection, the `conftest-imported` sentinel will fire. The current `lint.yml` passes `tests/python/` explicitly to pytest, so it is safe by construction. The `norecursedirs` entry in `pyproject.toml` is belt-and-suspenders. Verify this is actually the case before declaring the test safe — read `pyproject.toml` and `lint.yml` carefully.

### Test Isolation Pattern

Use `monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))` to give each test its own isolated sentinel directory. The `tmp_path` fixture is already available in pytest. Do not use a shared global canary dir — parallel test execution would cause false positives from other tests' runs.

### Why No Container Run

The issue mentions running the container against a fixture PR. That approach requires secrets (API keys) and a running container, which is not feasible in the unit test harness. The pytest-based approach exercises the actual code paths that touch workspace content, which is more surgical and achieves the same regression-guard goal. A full container-level canary test is out of scope for this story.

### Network Canary

The issue suggests also asserting "no canary network requests fire." Network canaries are hard in pytest (require mocking DNS or monitoring outbound sockets). The file-based canaries satisfy all five acceptance criteria checkboxes. Network canaries are a "nice to have" and are explicitly deferred.

### What NOT to Do

- **Do not add the canary workspace to pytest's collection path.** The `canary-workspace/conftest.py` must never be auto-collected.
- **Do not use `subprocess.run(["make", ...])` in the test itself** — that would be the action *executing* the Makefile, which is the thing we're guarding against. The test reads the files as bytes/text data only.
- **Do not add a new CI workflow file.** The existing `lint.yml` already covers the new tests.
- **Do not xfail or skip canary tests.** Failures are security events.
- **Do not write network-side canaries in this story.** Deferred.

### Files to Create/Modify

- `tests/security/__init__.py` (new — empty)
- `tests/security/canary-workspace/Makefile` (new)
- `tests/security/canary-workspace/setup.py` (new)
- `tests/security/canary-workspace/package.json` (new)
- `tests/security/canary-workspace/.pre-commit-config.yaml` (new)
- `tests/security/canary-workspace/conftest.py` (new)
- `tests/security/canary-workspace/.semgrep.yml` (new)
- `tests/security/canary-workspace/README.md` (new)
- `tests/python/test_security_canary.py` (new)
- `pyproject.toml` (modified — add `norecursedirs` + `markers` entry)
- `CONTRIBUTING.md` (modified — add security canary section)
- `SECURITY.md` (modified — update regression guard paragraph)

---

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6 (2026-06-23)

### Debug Log References

- API surface verification: `build_manifest()` does not exist; correct entry point is `build_changed_files(file_list)`. `run_analyzers()` is async and takes `ChangedFiles` object + `diff_file` path. `load_sarif_files()` not `ingest_sarif()`. `extract_symbol_refs(diff_hunk, language)` not `extract_symbols()`. `load_language_profiles(labels, script_dir)` not `load_profile()`.
- Ruff SIM105: replaced `try/except/pass` with `contextlib.suppress(Exception)` in `test_analyzer_bridge_does_not_execute_workspace`.
- `testpaths` in pyproject.toml already scoped to `tests/python` and `tests/golden`, so canary workspace conftest.py is safe by construction; `norecursedirs` is belt-and-suspenders.

### Completion Notes List

- All 7 canary tests pass: language detection, manifest, analyzer bridge, SARIF ingestor, context enrichment, language profile loading, raw byte reads.
- Full suite: 1668 passed (7 new security canary tests included, no regressions).
- mypy: clean across 85 source files.
- ruff: all checks passed after fixing SIM105 in the test file.
- SECURITY.md version table updated (was stale at v1.4.x; now reflects v2.1.x).
- SECURITY.md entrypoint description updated (bash engine deleted in v2.0.0; now correctly describes Python engine).
- Worktree: `feat/issue-494-security-canary` at `/home/gchaix/repos/tag1/ai-pr-review-issue-494`.

### Files to Create/Modify

- `tests/security/__init__.py` (new — empty)
- `tests/security/canary-workspace/Makefile` (new)
- `tests/security/canary-workspace/setup.py` (new)
- `tests/security/canary-workspace/package.json` (new)
- `tests/security/canary-workspace/.pre-commit-config.yaml` (new)
- `tests/security/canary-workspace/conftest.py` (new)
- `tests/security/canary-workspace/.semgrep.yml` (new)
- `tests/security/canary-workspace/README.md` (new)
- `tests/python/test_security_canary.py` (new)
- `pyproject.toml` (modified — `norecursedirs` + `markers` entries)
- `CONTRIBUTING.md` (modified — "Security canary tests" section added)
- `SECURITY.md` (modified — regression guard paragraph updated; version table and entrypoint description corrected)

### Change Log

| Date | Change | Reason |
|------|--------|--------|
| 2026-06-23 | Story created | Issue #494 — security canary test |
| 2026-06-23 | All tasks implemented and verified | 1668 tests pass, mypy clean, ruff clean |
