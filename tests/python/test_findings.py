"""Tests for findings pipeline (extract, merge, suppress)."""

from __future__ import annotations

import pytest

from ai_pr_review.findings.extract import extract_findings
from ai_pr_review.findings.merge import merge_findings
from ai_pr_review.findings.models import Finding


def _make_finding(**kw: object) -> Finding:
    defaults: dict[str, object] = {
        "severity": "High",
        "confidence": 80,
        "finding": "Test finding",
        "source": "test",
        "file": "foo.py",
        "line": 10,
    }
    return Finding(**(defaults | kw))


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


def test_extract_findings_normalise_severity() -> None:
    output = """
```json-findings
[{"severity": "high", "confidence": 80, "finding": "test"}]
```
"""
    findings = extract_findings(output, "agent")
    assert findings[0].severity == "High"


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


# ---------------------------------------------------------------------------
# Finding model validation
# ---------------------------------------------------------------------------


def test_finding_rejects_invalid_severity() -> None:
    with pytest.raises(Exception):
        Finding(severity="Extreme", confidence=80, finding="x")  # type: ignore[arg-type]


def test_finding_rejects_confidence_out_of_range() -> None:
    with pytest.raises(Exception):
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
