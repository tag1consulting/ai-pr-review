"""Tests for ai_pr_review.analyzers.sarif — E3.S5."""

import json
import tempfile
from pathlib import Path

import pytest

from ai_pr_review.analyzers.sarif import _sanitize_sarif_path, load_sarif_files


def _write_sarif(data: object, suffix: str = ".sarif") -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        json.dump(data, f)
        return f.name


def _minimal_sarif(
    driver_name: str = "testlint",
    results: list[dict] | None = None,
    rules: list[dict] | None = None,
) -> dict:
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": driver_name,
                        "rules": rules or [],
                    }
                },
                "results": results or [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_basic_error_result() -> None:
    sarif = _minimal_sarif(
        results=[
            {
                "level": "error",
                "message": {"text": "Null pointer dereference"},
                "ruleId": "NPD001",
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "src/main.py"},
                            "region": {"startLine": 42},
                        }
                    }
                ],
            }
        ]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "High"
    assert f.source == "sarif:testlint"
    assert f.file == "src/main.py"
    assert f.line == 42
    assert "NPD001" in f.finding
    assert "Null pointer dereference" in f.finding
    assert f.confidence == 90


def test_warning_maps_to_medium() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "warning", "message": {"text": "Style issue"}}]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings[0].severity == "Medium"


def test_note_maps_to_low() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "note", "message": {"text": "Suggestion"}}]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings[0].severity == "Low"


def test_none_level_maps_to_low() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "none", "message": {"text": "Informational"}}]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings[0].severity == "Low"


def test_missing_level_defaults_to_medium() -> None:
    sarif = _minimal_sarif(
        results=[{"message": {"text": "No level"}}]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings[0].severity == "Medium"


def test_rule_help_used_as_remediation() -> None:
    sarif = _minimal_sarif(
        rules=[{"id": "SEC001", "help": {"text": "Use parameterized queries."}}],
        results=[{"ruleId": "SEC001", "message": {"text": "SQL injection"}}],
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings[0].remediation == "Use parameterized queries."


def test_file_uri_prefix_stripped() -> None:
    sarif = _minimal_sarif(
        results=[
            {
                "message": {"text": "issue"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "file:///workspace/src/x.py"},
                        }
                    }
                ],
            }
        ]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    # No GITHUB_WORKSPACE/AI_SARIF_HOST_WORKSPACE configured and this path
    # doesn't match the runner-path regex fallback either, so it correctly
    # stays absolute (#846 review) rather than being silently truncated to a
    # relative-looking string scope.py's tripwire could never catch.
    assert findings[0].file == "/workspace/src/x.py"


def test_multiple_runs_merged() -> None:
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "lintA", "rules": []}},
                "results": [{"message": {"text": "A"}}],
            },
            {
                "tool": {"driver": {"name": "lintB", "rules": []}},
                "results": [{"message": {"text": "B"}}],
            },
        ],
    }
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert len(findings) == 2
    sources = {f.source for f in findings}
    assert "sarif:lintA" in sources
    assert "sarif:lintB" in sources


def test_multiple_files_aggregated() -> None:
    sarif_a = _minimal_sarif(driver_name="A", results=[{"message": {"text": "a1"}}])
    sarif_b = _minimal_sarif(driver_name="B", results=[{"message": {"text": "b1"}}])
    path_a = _write_sarif(sarif_a)
    path_b = _write_sarif(sarif_b)
    findings, _ = load_sarif_files([path_a, path_b])
    assert len(findings) == 2


# ---------------------------------------------------------------------------
# Fail-soft / edge cases
# ---------------------------------------------------------------------------

def test_missing_file_returns_empty(tmp_path: Path) -> None:
    findings, _ = load_sarif_files([str(tmp_path / "nonexistent.sarif")])
    assert findings == []


def test_invalid_json_returns_empty() -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sarif", delete=False, encoding="utf-8"
    ) as f:
        f.write("NOT JSON{")
        path = f.name
    findings, _ = load_sarif_files([path])
    assert findings == []


def test_non_dict_root_returns_empty() -> None:
    path = _write_sarif([1, 2, 3])
    findings, _ = load_sarif_files([path])
    assert findings == []


def test_no_runs_key_returns_empty() -> None:
    path = _write_sarif({"version": "2.1.0"})
    findings, _ = load_sarif_files([path])
    assert findings == []


def test_empty_message_skips_result() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "error", "message": {"text": ""}}]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings == []


def test_result_without_location_has_empty_file() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "warning", "message": {"text": "global issue"}}]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert findings[0].file == ""
    assert findings[0].line is None


def test_load_empty_paths_list() -> None:
    findings, elapsed = load_sarif_files([])
    assert findings == []
    assert elapsed >= 0.0


def test_load_sarif_files_returns_elapsed() -> None:
    findings, elapsed = load_sarif_files([])
    assert isinstance(findings, list)
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0

    # Nonexistent file: fail-soft returns empty list with timing
    findings2, elapsed2 = load_sarif_files(["nonexistent_file.sarif"])
    assert findings2 == []
    assert elapsed2 >= 0.0


# ---------------------------------------------------------------------------
# Path sanitization — security defense
# ---------------------------------------------------------------------------


def test_sanitize_sarif_path_rejects_absolute_path() -> None:
    """Absolute path with no scheme (e.g. /etc/passwd) must be rejected."""
    assert _sanitize_sarif_path("/etc/passwd") == ""


def test_sanitize_sarif_path_rejects_dot_dot_traversal() -> None:
    assert _sanitize_sarif_path("../../../etc/passwd") == ""
    assert _sanitize_sarif_path("src/../../etc/passwd") == ""


def test_sanitize_sarif_path_accepts_relative_path() -> None:
    assert _sanitize_sarif_path("src/main.py") == "src/main.py"


def test_sanitize_sarif_path_strips_file_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    # file:///abs/path with a matching GITHUB_WORKSPACE → repo-relative
    monkeypatch.setenv("GITHUB_WORKSPACE", "/workspace")
    assert _sanitize_sarif_path("file:///workspace/src/x.py") == "src/x.py"


def test_sanitize_sarif_path_drops_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """file://hostname/path must not leave 'hostname' in the result, and the
    scheme itself is still stripped even when nothing else matches."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.delenv("AI_SARIF_HOST_WORKSPACE", raising=False)
    result = _sanitize_sarif_path("file://hostname/path/x.py")
    assert "hostname" not in result
    # Nothing configured to strip this prefix against, so it correctly stays
    # absolute (#846 review) rather than masquerading as a relative path.
    assert result == "/path/x.py"


def test_sanitize_sarif_path_rejects_unknown_scheme() -> None:
    assert _sanitize_sarif_path("http://evil.example/x.py") == ""
    assert _sanitize_sarif_path("https://evil.example/x.py") == ""


def test_sanitize_sarif_path_handles_percent_encoding() -> None:
    """URLs with %20 etc. must decode correctly."""
    assert _sanitize_sarif_path("src/my%20file.py") == "src/my file.py"


def test_sanitize_sarif_path_empty_input() -> None:
    assert _sanitize_sarif_path("") == ""


def test_sanitize_sarif_path_rejects_extra_leading_slashes() -> None:
    """Regression: lstrip('/') would accept file:////etc/passwd as 'etc/passwd'
    bypassing the absolute-path check.  Must reject."""
    assert _sanitize_sarif_path("file:////etc/passwd") == ""
    assert _sanitize_sarif_path("file://///abs/path") == ""


def test_sanitize_sarif_path_strips_github_actions_workspace_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruff emits file:///home/runner/work/<owner>/<repo>/src/foo.py.
    After scheme stripping this is home/runner/work/<owner>/<repo>/src/foo.py,
    which must be reduced to src/foo.py to match diff paths.

    GITHUB_WORKSPACE is explicitly unset here so this exercises the
    regex-fallback path (no GITHUB_WORKSPACE prefix to match against)."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    uri = "file:///home/runner/work/tag1consulting/ai-pr-review/ai_pr_review/foo.py"
    assert _sanitize_sarif_path(uri) == "ai_pr_review/foo.py"


def test_sanitize_sarif_path_strips_runner_prefix_without_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some runner configurations omit /home, giving runner/work/... directly."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    uri = "file:///runner/work/myorg/myrepo/src/bar.py"
    assert _sanitize_sarif_path(uri) == "src/bar.py"


def test_sanitize_sarif_path_does_not_strip_non_runner_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paths that look like workspace dirs but aren't the runner pattern
    must not be silently truncated -- and, if genuinely unresolvable, must
    stay absolute rather than being handed back as a relative-looking
    string (#846 review)."""
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    monkeypatch.delenv("AI_SARIF_HOST_WORKSPACE", raising=False)
    # Plain relative path — unchanged
    assert _sanitize_sarif_path("ai_pr_review/sarif_smoke_test.py") == "ai_pr_review/sarif_smoke_test.py"
    # file:// with a non-runner absolute path and nothing to strip it against
    # — stays absolute (scheme/leading-slash accounting only, no truncation)
    assert _sanitize_sarif_path("file:///workspace/src/x.py") == "/workspace/src/x.py"


def test_sanitize_sarif_path_strips_github_workspace_prefix_self_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #846: a self-hosted runner can check out anywhere, so the
    hardcoded runner/work/<owner>/<repo>/ regex never matches its paths.
    GITHUB_WORKSPACE names the real checkout root on the composite
    (non-container) action, and must be preferred over the regex heuristic
    when it matches. (This engine's container-action variant remaps
    GITHUB_WORKSPACE inside the container -- see AI_SARIF_HOST_WORKSPACE
    below for that case.)"""
    monkeypatch.setenv("GITHUB_WORKSPACE", "/opt/actions-runner/_work/ai-pr-review/ai-pr-review")
    uri = "file:///opt/actions-runner/_work/ai-pr-review/ai-pr-review/ai_pr_review/foo.py"
    assert _sanitize_sarif_path(uri) == "ai_pr_review/foo.py"


def test_sanitize_sarif_path_ai_sarif_host_workspace_takes_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#846 review: under container-action, GITHUB_WORKSPACE is remapped to
    the container's own mount point (e.g. /workspace) and can never match a
    SARIF file's host-rooted path. AI_SARIF_HOST_WORKSPACE lets a caller
    supply the real host checkout root for exactly that case, and it must
    be tried before GITHUB_WORKSPACE."""
    monkeypatch.setenv("AI_SARIF_HOST_WORKSPACE", "/opt/actions-runner/_work/ai-pr-review/ai-pr-review")
    monkeypatch.setenv("GITHUB_WORKSPACE", "/workspace")
    uri = "file:///opt/actions-runner/_work/ai-pr-review/ai-pr-review/ai_pr_review/foo.py"
    assert _sanitize_sarif_path(uri) == "ai_pr_review/foo.py"


def test_sanitize_sarif_path_relative_uri_never_workspace_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#846 review: an already-relative SARIF URI must never be treated as a
    workspace-prefix candidate, even when it happens to start with a
    segment matching GITHUB_WORKSPACE's own basename -- otherwise a
    genuinely relative path under a top-level `workspace/` directory
    (plausible in Cargo/pnpm/Bazel-style monorepos) gets its first segment
    silently truncated."""
    monkeypatch.setenv("GITHUB_WORKSPACE", "/workspace")
    assert _sanitize_sarif_path("workspace/src/x.py") == "workspace/src/x.py"
    assert _sanitize_sarif_path("workspace/x.py") == "workspace/x.py"


def test_sanitize_sarif_path_github_workspace_takes_priority_over_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When GITHUB_WORKSPACE is set and matches, it must be used -- and this
    must genuinely be the branch doing the work, not the regex fallback
    coincidentally succeeding on the same input (#846 review: the original
    version of this test used a runner-shaped path that the regex alone
    would also resolve correctly, so it passed even with the whole
    GITHUB_WORKSPACE branch deleted). A self-hosted-style checkout root the
    regex cannot match ensures only the GITHUB_WORKSPACE branch can produce
    the correct result here."""
    monkeypatch.setenv("GITHUB_WORKSPACE", "/opt/self-hosted-runner/_work/ai-pr-review")
    uri = "file:///opt/self-hosted-runner/_work/ai-pr-review/ai_pr_review/foo.py"
    assert _sanitize_sarif_path(uri) == "ai_pr_review/foo.py"


def test_sanitize_sarif_path_github_workspace_mismatch_falls_back_to_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GITHUB_WORKSPACE that doesn't prefix-match the given path (e.g. a
    stale/misconfigured env, or a path from a different job) must fall
    through to the regex heuristic rather than short-circuiting on the
    mismatch and leaving the path untouched. (This specific input is also
    resolvable by the regex alone -- see the priority test above for the
    complementary case that isolates the GITHUB_WORKSPACE branch itself --
    so this test's real value is guarding the fall-through logic, not
    branch selection: a buggy mismatch handler that raises, returns early,
    or corrupts the path on a partial/non-prefix match would fail here.)"""
    monkeypatch.setenv("GITHUB_WORKSPACE", "/some/other/checkout/root")
    uri = "file:///home/runner/work/tag1consulting/ai-pr-review/ai_pr_review/foo.py"
    assert _sanitize_sarif_path(uri) == "ai_pr_review/foo.py"


def test_finding_with_traversal_uri_drops_file_field() -> None:
    """End-to-end: a SARIF result with '../../etc/passwd' should produce a
    Finding with file="" (not the traversal path)."""
    sarif = _minimal_sarif(
        results=[
            {
                "message": {"text": "bad path"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "../../etc/passwd"},
                        }
                    }
                ],
            }
        ]
    )
    path = _write_sarif(sarif)
    findings, _ = load_sarif_files([path])
    assert len(findings) == 1
    assert findings[0].file == "", "traversal path must be dropped"


# ---------------------------------------------------------------------------
# Warning format (fail-soft path 4-5: Story 4-5)
# ---------------------------------------------------------------------------


def test_unreadable_sarif_logs_warning(tmp_path, caplog):
    import logging

    missing = tmp_path / "ghost.sarif"
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.analyzers.sarif"):
        result, _ = load_sarif_files([str(missing)])
    assert result == []
    assert any("[ai-pr-review] WARNING:" in r.message for r in caplog.records)


def test_invalid_json_sarif_logs_warning(tmp_path, caplog):
    import logging

    bad = tmp_path / "bad.sarif"
    bad.write_text("NOT JSON{", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.analyzers.sarif"):
        result, _ = load_sarif_files([str(bad)])
    assert result == []
    assert any("[ai-pr-review] WARNING:" in r.message for r in caplog.records)
