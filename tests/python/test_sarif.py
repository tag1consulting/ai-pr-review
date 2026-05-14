"""Tests for ai_pr_review.analyzers.sarif — E3.S5."""

import json
import tempfile
from pathlib import Path

import pytest

from ai_pr_review.analyzers.sarif import load_sarif_files, _parse_sarif_file


def _write_sarif(data: object, suffix: str = ".sarif") -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    )
    json.dump(data, f)
    f.close()
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
    findings = load_sarif_files([path])
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
    findings = load_sarif_files([path])
    assert findings[0].severity == "Medium"


def test_note_maps_to_low() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "note", "message": {"text": "Suggestion"}}]
    )
    path = _write_sarif(sarif)
    findings = load_sarif_files([path])
    assert findings[0].severity == "Low"


def test_none_level_maps_to_low() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "none", "message": {"text": "Informational"}}]
    )
    path = _write_sarif(sarif)
    findings = load_sarif_files([path])
    assert findings[0].severity == "Low"


def test_missing_level_defaults_to_medium() -> None:
    sarif = _minimal_sarif(
        results=[{"message": {"text": "No level"}}]
    )
    path = _write_sarif(sarif)
    findings = load_sarif_files([path])
    assert findings[0].severity == "Medium"


def test_rule_help_used_as_remediation() -> None:
    sarif = _minimal_sarif(
        rules=[{"id": "SEC001", "help": {"text": "Use parameterized queries."}}],
        results=[{"ruleId": "SEC001", "message": {"text": "SQL injection"}}],
    )
    path = _write_sarif(sarif)
    findings = load_sarif_files([path])
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
    findings = load_sarif_files([path])
    assert findings[0].file == "workspace/src/x.py"


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
    findings = load_sarif_files([path])
    assert len(findings) == 2
    sources = {f.source for f in findings}
    assert "sarif:lintA" in sources
    assert "sarif:lintB" in sources


def test_multiple_files_aggregated() -> None:
    sarif_a = _minimal_sarif(driver_name="A", results=[{"message": {"text": "a1"}}])
    sarif_b = _minimal_sarif(driver_name="B", results=[{"message": {"text": "b1"}}])
    path_a = _write_sarif(sarif_a)
    path_b = _write_sarif(sarif_b)
    findings = load_sarif_files([path_a, path_b])
    assert len(findings) == 2


# ---------------------------------------------------------------------------
# Fail-soft / edge cases
# ---------------------------------------------------------------------------

def test_missing_file_returns_empty(tmp_path: Path) -> None:
    findings = load_sarif_files([str(tmp_path / "nonexistent.sarif")])
    assert findings == []


def test_invalid_json_returns_empty() -> None:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sarif", delete=False, encoding="utf-8"
    )
    f.write("NOT JSON{")
    f.close()
    findings = load_sarif_files([f.name])
    assert findings == []


def test_non_dict_root_returns_empty() -> None:
    path = _write_sarif([1, 2, 3])
    findings = load_sarif_files([path])
    assert findings == []


def test_no_runs_key_returns_empty() -> None:
    path = _write_sarif({"version": "2.1.0"})
    findings = load_sarif_files([path])
    assert findings == []


def test_empty_message_skips_result() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "error", "message": {"text": ""}}]
    )
    path = _write_sarif(sarif)
    findings = load_sarif_files([path])
    assert findings == []


def test_result_without_location_has_empty_file() -> None:
    sarif = _minimal_sarif(
        results=[{"level": "warning", "message": {"text": "global issue"}}]
    )
    path = _write_sarif(sarif)
    findings = load_sarif_files([path])
    assert findings[0].file == ""
    assert findings[0].line is None


def test_load_empty_paths_list() -> None:
    assert load_sarif_files([]) == []


# ---------------------------------------------------------------------------
# Path sanitization — security defense
# ---------------------------------------------------------------------------

from ai_pr_review.analyzers.sarif import _sanitize_sarif_path


def test_sanitize_sarif_path_rejects_absolute_path() -> None:
    """Absolute path with no scheme (e.g. /etc/passwd) must be rejected."""
    assert _sanitize_sarif_path("/etc/passwd") == ""


def test_sanitize_sarif_path_rejects_dot_dot_traversal() -> None:
    assert _sanitize_sarif_path("../../../etc/passwd") == ""
    assert _sanitize_sarif_path("src/../../etc/passwd") == ""


def test_sanitize_sarif_path_accepts_relative_path() -> None:
    assert _sanitize_sarif_path("src/main.py") == "src/main.py"


def test_sanitize_sarif_path_strips_file_scheme() -> None:
    # file:///abs/path → abs/path (stripped leading slash)
    assert _sanitize_sarif_path("file:///workspace/src/x.py") == "workspace/src/x.py"


def test_sanitize_sarif_path_drops_authority() -> None:
    """file://hostname/path must not leave 'hostname' in the result."""
    result = _sanitize_sarif_path("file://hostname/path/x.py")
    assert "hostname" not in result
    assert result == "path/x.py"


def test_sanitize_sarif_path_rejects_unknown_scheme() -> None:
    assert _sanitize_sarif_path("http://evil.example/x.py") == ""
    assert _sanitize_sarif_path("https://evil.example/x.py") == ""


def test_sanitize_sarif_path_handles_percent_encoding() -> None:
    """URLs with %20 etc. must decode correctly."""
    assert _sanitize_sarif_path("src/my%20file.py") == "src/my file.py"


def test_sanitize_sarif_path_empty_input() -> None:
    assert _sanitize_sarif_path("") == ""


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
    findings = load_sarif_files([path])
    assert len(findings) == 1
    assert findings[0].file == "", "traversal path must be dropped"
