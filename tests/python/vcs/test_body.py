"""Tests for ai_pr_review.vcs._body."""

from __future__ import annotations

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import (
    GITHUB_MAX_BODY_SIZE,
    build_agent_prompt,
    format_body_finding,
    format_source_tag,
    severity_icon,
    truncate_body,
)


def test_severity_icon_known() -> None:
    assert severity_icon("Critical") == "🚨"
    assert severity_icon("high") == "🔴"
    assert severity_icon("Medium") == "🟡"
    assert severity_icon("low") == "🔵"


def test_severity_icon_unknown_defaults() -> None:
    assert severity_icon("weird") == "🔵"


def test_format_source_tag_sources_preferred() -> None:
    f = Finding(
        severity="Low",
        confidence=80,
        finding="x",
        source="solo",
        sources=["a", "b"],
    )
    assert format_source_tag(f) == "[a, b]"


def test_format_source_tag_falls_back_to_source() -> None:
    f = Finding(severity="Low", confidence=80, finding="x", source="solo")
    assert format_source_tag(f) == "[solo]"


def test_format_source_tag_empty() -> None:
    f = Finding(severity="Low", confidence=80, finding="x")
    assert format_source_tag(f) == ""


def test_format_body_finding_full() -> None:
    f = Finding(
        severity="High",
        confidence=90,
        finding="SQLi in user input",
        source="blind-hunter",
        file="app/db.py",
        line=42,
        remediation="Use parameterized queries",
    )
    out = format_body_finding(f)
    assert "🔴" in out
    assert "[High]" in out
    assert "[blind-hunter]" in out
    assert "SQLi" in out
    assert "app/db.py:42" in out
    assert "**Remediation:**" in out
    assert "parameterized" in out


def test_format_body_finding_with_location_note() -> None:
    f = Finding(severity="Low", confidence=50, finding="x", file="a.py", line=1)
    out = format_body_finding(f, location_note=" *(line not in diff)*")
    assert "(line not in diff)" in out


def test_truncate_body_under_limit_unchanged() -> None:
    body = "hello"
    assert truncate_body(body) == body


def test_truncate_body_over_limit() -> None:
    body = "x" * (GITHUB_MAX_BODY_SIZE + 100)
    out = truncate_body(body)
    assert len(out.encode("utf-8")) <= GITHUB_MAX_BODY_SIZE + 500
    assert "truncated" in out.lower()


def test_truncate_body_utf8_safe() -> None:
    # Force a cut that lands mid-UTF-8-sequence by using a small limit.
    body = "aaaa" + "é" * 100  # é is 2 bytes in UTF-8
    out = truncate_body(body, limit=5)
    # Must still decode cleanly
    out.encode("utf-8").decode("utf-8")
    assert out.startswith("aaaa")
    assert "truncated" in out.lower()


def test_build_agent_prompt_empty() -> None:
    assert build_agent_prompt([]) == ""


def test_build_agent_prompt_renders_collapsible_block() -> None:
    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="issue",
            file="a.py",
            line=3,
            remediation="fix it",
        ),
    ]
    out = build_agent_prompt(findings)
    assert "<details>" in out
    assert "Prompt for AI agents" in out
    assert "```json" in out
    assert "a.py:3" in out
    assert "fix it" in out
