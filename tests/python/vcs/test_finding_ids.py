"""Tests for ai_pr_review.vcs._finding_ids."""

from __future__ import annotations

import hashlib

import pytest

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs._finding_ids import assemble_id_map, fingerprint


def _finding(
    text: str,
    source: str = "code-reviewer",
    file: str = "app.py",
    line: int | None = 10,
    severity: str = "medium",
) -> Finding:
    return Finding(
        severity=severity,
        confidence=80,
        finding=text,
        source=source,
        file=file,
        line=line,
    )


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------

def test_fingerprint_stable_across_calls() -> None:
    f = _finding("SQL injection in user input")
    assert fingerprint(f) == fingerprint(f)


def test_fingerprint_varies_by_text() -> None:
    f1 = _finding("SQL injection")
    f2 = _finding("XSS in template")
    assert fingerprint(f1) != fingerprint(f2)


def test_fingerprint_varies_by_source() -> None:
    f1 = _finding("x", source="code-reviewer")
    f2 = _finding("x", source="security-reviewer")
    assert fingerprint(f1) != fingerprint(f2)


def test_fingerprint_varies_by_file() -> None:
    f1 = _finding("x", file="a.py")
    f2 = _finding("x", file="b.py")
    assert fingerprint(f1) != fingerprint(f2)


def test_fingerprint_varies_by_line() -> None:
    f1 = _finding("x", line=1)
    f2 = _finding("x", line=2)
    assert fingerprint(f1) != fingerprint(f2)


def test_fingerprint_none_line() -> None:
    f = _finding("x", line=None)
    fp = fingerprint(f)
    assert fp  # non-empty, doesn't crash


# ---------------------------------------------------------------------------
# assemble_id_map() — no prior reviews
# ---------------------------------------------------------------------------

def test_first_review_starts_at_one() -> None:
    findings = [_finding("A"), _finding("B"), _finding("C")]
    id_map = assemble_id_map([], findings)
    # All three should get IDs 1, 2, 3 in order
    ids = [id_map[fingerprint(f)] for f in findings]
    assert ids == [1, 2, 3]


def test_empty_findings_returns_empty_map() -> None:
    assert assemble_id_map([], []) == {}


# ---------------------------------------------------------------------------
# assemble_id_map() — with prior reviews
# ---------------------------------------------------------------------------

def _render_body(findings: list[Finding], prior_bodies: list[str] = []) -> str:
    """Render a body-findings section for test fixtures."""
    id_map = assemble_id_map(prior_bodies, findings)
    bullets = [
        format_body_finding(f, finding_id=id_map.get(fingerprint(f)))
        for f in findings
    ]
    return "### Findings not attached to specific lines\n" + "\n".join(bullets)


def test_existing_ids_preserved_across_reviews() -> None:
    fa = _finding("A")
    fb = _finding("B")
    fc = _finding("C")

    # First review: all three findings
    body_a = _render_body([fa, fb, fc])

    # Second review: same three findings re-detected
    id_map = assemble_id_map([body_a], [fa, fb, fc])
    assert id_map[fingerprint(fa)] == 1
    assert id_map[fingerprint(fb)] == 2
    assert id_map[fingerprint(fc)] == 3


def test_dismissed_gap_preserved_new_finding_gets_next_after_max() -> None:
    fa = _finding("A")
    fb = _finding("B")
    fc = _finding("C")

    # First review: F1, F2, F3
    body_a = _render_body([fa, fb, fc])

    # Second review: F2 was dismissed (suppressed), new finding FD appears
    fd = _finding("D")
    id_map = assemble_id_map([body_a], [fa, fc, fd])

    assert id_map[fingerprint(fa)] == 1
    assert id_map[fingerprint(fc)] == 3
    assert id_map[fingerprint(fd)] == 4  # max was 3; next is 4


def test_new_finding_in_second_review_gets_next_id() -> None:
    fa = _finding("A")
    body_a = _render_body([fa])  # F1

    fb = _finding("B")  # new finding in second review
    id_map = assemble_id_map([body_a], [fa, fb])

    assert id_map[fingerprint(fa)] == 1
    assert id_map[fingerprint(fb)] == 2


def test_monotonic_across_three_reviews() -> None:
    fa = _finding("A")
    fb = _finding("B")

    body1 = _render_body([fa])        # F1
    body2 = _render_body([fa, fb], [body1])   # F1, F2

    fc = _finding("C")
    id_map = assemble_id_map([body1, body2], [fa, fb, fc])
    assert id_map[fingerprint(fa)] == 1
    assert id_map[fingerprint(fb)] == 2
    assert id_map[fingerprint(fc)] == 3


def test_unparseable_lines_skipped() -> None:
    # A body with malformed or unrelated lines — should not crash, just skip
    body = (
        "### Findings not attached to specific lines\n"
        "- this line has no ID token\n"
        "some random text\n"
        "- 🟡 **[medium]** **[F5]** [agent] a real finding *(at `x.py:1`)*\n"
    )
    fa = _finding("brand new finding")
    id_map = assemble_id_map([body], [fa])
    # F5 was the max; new finding gets F6
    assert id_map[fingerprint(fa)] == 6


# ---------------------------------------------------------------------------
# format_body_finding() with finding_id
# ---------------------------------------------------------------------------

def test_format_body_finding_with_id_emits_token() -> None:
    f = _finding("SQL injection")
    out = format_body_finding(f, finding_id=1)
    assert "**[F1]**" in out


def test_format_body_finding_id_between_severity_and_source() -> None:
    f = _finding("SQL injection")
    out = format_body_finding(f, finding_id=2)
    # Severity is rendered as title-case: **[Medium]**
    severity_pos = out.index("[Medium]")
    id_pos = out.index("[F2]")
    source_pos = out.index("[code-reviewer]")
    assert severity_pos < id_pos < source_pos


def test_format_body_finding_without_id_no_ftoken() -> None:
    f = _finding("SQL injection")
    out = format_body_finding(f)
    assert "[F" not in out


def test_format_body_finding_id_roundtrips_through_id_map() -> None:
    """IDs assigned via assemble_id_map must survive a parse-and-reassign cycle."""
    fa = _finding("A")
    fb = _finding("B")

    id_map1 = assemble_id_map([], [fa, fb])
    body1 = "### Findings not attached to specific lines\n" + "\n".join(
        format_body_finding(f, finding_id=id_map1[fingerprint(f)]) for f in [fa, fb]
    )

    id_map2 = assemble_id_map([body1], [fa, fb])
    assert id_map2 == id_map1
