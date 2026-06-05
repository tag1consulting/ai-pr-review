"""Tests for findings pipeline (extract, merge, suppress)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_pr_review.findings.extract import extract_findings
from ai_pr_review.findings.merge import merge_findings
from ai_pr_review.findings.models import Finding

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _make_finding(**kw: object) -> Finding:
    defaults: dict[str, object] = {
        "severity": "High",
        "confidence": 80,
        "finding": "Test finding",
        "source": "test",
        "file": "foo.py",
        "line": 10,
    }
    return Finding.model_validate(defaults | kw)


# ---------------------------------------------------------------------------
# extract_findings
# ---------------------------------------------------------------------------


def test_extract_findings_basic() -> None:
    output = """
Some preamble text.

```json-findings
[
  {
    "severity": "High",
    "confidence": 85,
    "file": "foo.py",
    "line": 42,
    "finding": "SQL injection risk",
    "source": "security-reviewer"
  }
]
```
"""
    findings = extract_findings(output, "security-reviewer")
    assert len(findings) == 1
    assert findings[0].severity == "High"
    assert findings[0].line == 42


def test_extract_findings_stamps_source() -> None:
    output = """
```json-findings
[{"severity": "Low", "confidence": 76, "finding": "style issue"}]
```
"""
    findings = extract_findings(output, "code-reviewer")
    assert findings[0].source == "code-reviewer"


def test_extract_findings_no_block_returns_empty() -> None:
    findings = extract_findings("No findings block here.", "agent")
    assert findings == []


def test_extract_findings_invalid_json_returns_empty() -> None:
    output = "```json-findings\n{bad json\n```\n"
    findings = extract_findings(output, "agent")
    assert findings == []


def test_extract_findings_truncated_no_fence_returns_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """truncated=True with no fence logs the truncation warning and returns []."""
    output = (_FIXTURES / "truncated-no-fence.md").read_text()
    findings = extract_findings(output, "code-reviewer", truncated=True)
    assert findings == []
    captured = capsys.readouterr()
    assert "truncated before json-findings block" in captured.err
    assert "code-reviewer" in captured.err


def test_extract_findings_truncated_json_salvaged(capsys: pytest.CaptureFixture[str]) -> None:
    """truncated=True with a fenced block whose JSON is internally incomplete.

    _try_repair walks backwards to the last complete '}'  and reconstructs the
    array, recovering findings that arrived before truncation.
    """
    # Two complete objects followed by a partial third (no closing brace/bracket).
    # The fence IS closed so _FENCE_RE can match, but json.loads fails on the
    # incomplete array -- triggering _try_repair.
    output = (
        "```json-findings\n"
        '[\n'
        '  {"severity":"High","confidence":85,"file":"foo.py","line":1,"finding":"f1"},\n'
        '  {"severity":"Medium","confidence":77,"file":"bar.py","line":2,"finding":"f2"},\n'
        '  {"severity":"Low","confidence":76,"file":"baz.py","line":3,"finding":"Partial find\n'
        "```\n"
    )
    findings = extract_findings(output, "code-reviewer", truncated=True)
    assert len(findings) >= 2
    assert all(f.confidence >= 75 for f in findings)
    captured = capsys.readouterr()
    assert "salvaged" in captured.err


def test_extract_findings_normalise_severity() -> None:
    output = """
```json-findings
[{"severity": "high", "confidence": 80, "finding": "test"}]
```
"""
    findings = extract_findings(output, "agent")
    assert findings[0].severity == "High"


def test_extract_findings_strips_out_of_diff_injection() -> None:
    """out_of_diff: true in agent JSON must be stripped to prevent suppression bypass."""
    output = """
```json-findings
[{"severity": "Critical", "confidence": 90, "finding": "real bug", "out_of_diff": true}]
```
"""
    findings = extract_findings(output, "security-reviewer")
    assert len(findings) == 1
    assert not findings[0].out_of_diff, (
        "out_of_diff injected via agent JSON must be reset to False by extract_findings"
    )
    assert findings[0].severity == "Critical"


# ---------------------------------------------------------------------------
# merge_findings / dedup
# ---------------------------------------------------------------------------


def test_merge_filters_by_confidence() -> None:
    findings = [
        _make_finding(confidence=74, line=5),   # below threshold — dropped
        _make_finding(confidence=75, line=50),  # at threshold — kept
        _make_finding(confidence=90, line=100), # above threshold — kept
    ]
    result = merge_findings(findings, confidence_threshold=75)
    assert len(result) == 2
    assert all(f.confidence >= 75 for f in result)


def test_merge_dedup_same_file_nearby_lines() -> None:
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="a.py", line=12, source="agent2"),  # within 3 lines
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert sorted(result[0].sources) == ["agent1", "agent2"]


def test_merge_dedup_preserves_distinct_nearby() -> None:
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="a.py", line=20, source="agent2"),  # 10 lines apart — distinct
    ]
    result = merge_findings(findings)
    assert len(result) == 2


def test_merge_dedup_different_files() -> None:
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="b.py", line=10, source="agent2"),
    ]
    result = merge_findings(findings)
    assert len(result) == 2


def test_merge_severity_order() -> None:
    findings = [
        _make_finding(file="a.py", line=1, severity="Low", confidence=80),
        _make_finding(file="a.py", line=100, severity="Critical", confidence=80),
        _make_finding(file="a.py", line=200, severity="Medium", confidence=80),
    ]
    result = merge_findings(findings)
    assert result[0].severity == "Critical"
    assert result[1].severity == "Medium"
    assert result[2].severity == "Low"


def test_merge_empty_input() -> None:
    assert merge_findings([]) == []


def test_merge_proximity_chaining() -> None:
    """Findings at lines 1, 3, 6 should all merge when PROXIMITY_LINES=3.

    Line 1 and 3 are within 3 (merged). Line 3 and 6 are within 3 (also
    merged). Without tail-comparison, line 6 would incorrectly be compared
    against line 1 (distance 5 > 3) and start a new cluster.
    """
    findings = [
        _make_finding(file="a.py", line=1, source="s1"),
        _make_finding(file="a.py", line=3, source="s2"),
        _make_finding(file="a.py", line=6, source="s3"),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert sorted(result[0].sources) == ["s1", "s2", "s3"]


# ---------------------------------------------------------------------------
# Finding model validation
# ---------------------------------------------------------------------------


def test_finding_rejects_invalid_severity() -> None:
    with pytest.raises(ValueError):
        Finding(severity="Extreme", confidence=80, finding="x")  # type: ignore[arg-type]


def test_finding_rejects_confidence_out_of_range() -> None:
    with pytest.raises(ValueError):
        Finding(severity="High", confidence=101, finding="x")


def test_finding_to_dict() -> None:
    f = Finding(
        severity="High",
        confidence=80,
        finding="test",
        source="s",
        file="f.py",
        line=5,
    )
    d = f.to_dict()
    assert d["severity"] == "High"
    assert d["file"] == "f.py"
    assert d["line"] == 5
