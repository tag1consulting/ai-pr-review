"""Tests for ai_pr_review.slash.dismiss — pure classification logic (no HTTP)."""

from __future__ import annotations

from ai_pr_review.findings.models import Finding
from ai_pr_review.slash.dismiss import (
    FindingLocation,
    classify_finding,
    context_from_body_finding_id,
    list_active_body_ids,
    parse_inline_comment_header,
)
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs.github import _build_inline_comment_body
from ai_pr_review.vcs.marker import build_id_map_marker


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


def test_body_bullet_in_diff_section_classifies_as_body() -> None:
    f = _finding("SQL injection", source="security-reviewer", file="db.py", line=42)
    bullet = format_body_finding(f, finding_id=3)
    body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    result = classify_finding([body], 3)

    assert result.location is FindingLocation.BODY
    assert result.source == "security-reviewer"
    assert result.file == "db.py"
    assert result.line == "42"


def test_out_of_diff_bullet_classifies_as_body() -> None:
    """Issue #550 regression: a finding rendered only in the out-of-diff
    <details> block (no in-diff heading at all) must still classify as BODY,
    not UNKNOWN. This is the exact bug class #553 fixed for ID reconstruction
    and this classifier must not reintroduce it."""
    f = _finding("pre-existing style issue", source="phpcs", file="legacy.py", line=99)
    bullet = format_body_finding(f, finding_id=7)
    body = (
        "<details>\n"
        "<summary>Out-of-diff analyzer findings (1) — pre-existing issues on unchanged lines, capped to Low</summary>\n\n"
        f"{bullet}\n"
        "</details>"
    )

    result = classify_finding([body], 7)

    assert result.location is FindingLocation.BODY
    assert result.source == "phpcs"
    assert result.file == "legacy.py"
    assert result.line == "99"


def test_inline_finding_classifies_via_id_map_not_bullet() -> None:
    """A finding present only in the id-map marker (never rendered as a body
    bullet, because it's an inline PR-line comment) must classify as INLINE."""
    id_map = {"security-reviewer|api.py|10|abc123456789": 4}
    body = "Some review body.\n" + build_id_map_marker(id_map)

    result = classify_finding([body], 4)

    assert result.location is FindingLocation.INLINE
    assert result.source == ""  # INLINE classification does not extract fields


def test_marker_present_does_not_prevent_body_bullet_classification() -> None:
    """Load-bearing case (AC 7): when a body carries BOTH the id-map marker
    (covering all buckets indiscriminately) AND a rendered body bullet for
    the same F-id, the bullet-scan must still run and win — classification
    must not be derived from `_parse_existing_ids`'s marker fast-path output,
    which cannot distinguish buckets."""
    f = _finding("XSS", source="security-reviewer", file="views.py", line=5)
    bullet = format_body_finding(f, finding_id=2)
    id_map = {"security-reviewer|views.py|5|deadbeefcafe": 2, "other|x.py|1|1111aaaa2222": 9}
    body = (
        "### Findings not attached to specific lines\n\n"
        + bullet
        + "\n"
        + build_id_map_marker(id_map)
    )

    result_body = classify_finding([body], 2)
    assert result_body.location is FindingLocation.BODY

    result_inline = classify_finding([body], 9)
    assert result_inline.location is FindingLocation.INLINE


def test_unknown_finding_id_classifies_as_unknown() -> None:
    body = "### Findings not attached to specific lines\n\nsome unrelated text\n"
    result = classify_finding([body], 999)
    assert result.location is FindingLocation.UNKNOWN


def test_severity_bracket_does_not_leak_into_source_extraction() -> None:
    """Issue #553 regression: the source tag must be extracted correctly even
    though a severity bracket like **[High]** precedes it in the same bullet
    — the bug fixed in #553 mis-extracted the severity string as the source."""
    f = _finding("hardcoded credential", source="security-reviewer", severity="high")
    bullet = format_body_finding(f, finding_id=1)
    body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    result = classify_finding([body], 1)

    assert result.location is FindingLocation.BODY
    assert result.source == "security-reviewer"
    assert result.source != "High"


def test_list_active_body_ids_returns_sorted_ids_across_bodies() -> None:
    f1 = _finding("A", file="a.py", line=1)
    f2 = _finding("B", file="b.py", line=2)
    f3 = _finding("C", file="c.py", line=3)
    body1 = (
        "### Findings not attached to specific lines\n\n"
        + format_body_finding(f1, finding_id=5)
        + "\n"
        + format_body_finding(f3, finding_id=1)
        + "\n"
    )
    body2 = (
        "<details>\n<summary>Out-of-diff analyzer findings (1)</summary>\n\n"
        + format_body_finding(f2, finding_id=3)
        + "\n</details>"
    )

    assert list_active_body_ids([body1, body2]) == [1, 3, 5]


def test_list_active_body_ids_empty_when_no_bullets() -> None:
    assert list_active_body_ids(["no findings here", ""]) == []


def test_classify_finding_scans_all_bodies_not_just_first() -> None:
    f = _finding("finding in second review", file="z.py", line=1)
    bullet = format_body_finding(f, finding_id=2)
    body1 = "### Findings not attached to specific lines\n\nunrelated\n"
    body2 = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    result = classify_finding([body1, body2], 2)

    assert result.location is FindingLocation.BODY


def test_parse_inline_comment_header_extracts_source_after_severity() -> None:
    """The rendered inline-comment header is `{icon} **[{severity}]**{id}
    {tag} {text}` — severity comes BEFORE the source tag (unlike a body
    bullet, which has no leading severity bracket), so the parser must skip
    the first bracket group, not take it."""
    f = _finding("XSS in template", source="security-reviewer", file="views.py", line=5, severity="high")
    body = _build_inline_comment_body(f, finding_id=3)

    result = parse_inline_comment_header(body)

    assert result.location is FindingLocation.INLINE
    assert result.source == "security-reviewer"
    assert result.rule_id == ""


def test_parse_inline_comment_header_extracts_sarif_rule_id() -> None:
    """Matches `_scan_body_bullets`'s existing convention: rule_id is the
    full source string for a sarif: source, not a separate bracket."""
    f = _finding("insecure hash", source="sarif:bandit", file="crypto.py", line=8)
    body = _build_inline_comment_body(f, finding_id=9)

    result = parse_inline_comment_header(body)

    assert result.source == "sarif:bandit"
    assert result.rule_id == "sarif:bandit"


def test_parse_inline_comment_header_multi_source_keeps_first_only() -> None:
    f = Finding(
        severity="medium",
        confidence=80,
        finding="dup finding",
        sources=["code-reviewer", "security-reviewer"],
        file="app.py",
        line=1,
    )
    body = _build_inline_comment_body(f, finding_id=1)

    result = parse_inline_comment_header(body)

    assert result.source == "code-reviewer"


def test_parse_inline_comment_header_no_id_token_still_parses() -> None:
    """finding_id is optional on the render; the id-strip regex must be a
    no-op (not consume anything) when there's no **[F<n>]** token."""
    f = _finding("plain finding", source="code-reviewer", file="app.py", line=1)
    body = _build_inline_comment_body(f, finding_id=None)

    result = parse_inline_comment_header(body)

    assert result.source == "code-reviewer"


def test_parse_inline_comment_header_unparseable_returns_unknown() -> None:
    result = parse_inline_comment_header("not a rendered finding at all")
    assert result.location is FindingLocation.UNKNOWN
    assert result.source == ""


def test_parse_inline_comment_header_empty_body_returns_unknown() -> None:
    result = parse_inline_comment_header("")
    assert result.location is FindingLocation.UNKNOWN


def test_context_from_body_finding_id_body_bucket() -> None:
    f = _finding("SQL injection", source="security-reviewer", file="db.py", line=42)
    bullet = format_body_finding(f, finding_id=3)
    body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    context = context_from_body_finding_id([body], 3)

    assert context.source == "security-reviewer"
    assert context.file == "db.py"
    assert context.missing_reason == ""
    assert context.notice == ""


def test_context_from_body_finding_id_inline_sets_notice_not_missing_reason() -> None:
    """Matches bash's `inline)` branch: an advisory notice, never a warning —
    the finding IS found, it's just in the wrong bucket for this lookup."""
    id_map = {"security-reviewer|api.py|10|abc123456789": 4}
    body = "Some review body.\n" + build_id_map_marker(id_map)

    context = context_from_body_finding_id([body], 4)

    assert context.source == ""
    assert context.missing_reason == ""
    assert "inline finding" in context.notice
    assert "F4" in context.notice


def test_context_from_body_finding_id_not_found_is_silent() -> None:
    """Matches bash's `not_found)` branch: completely empty, no notice or
    warning — a plain miss is not noteworthy."""
    context = context_from_body_finding_id(["no findings here"], 999)

    assert context.source == ""
    assert context.missing_reason == ""
    assert context.notice == ""
