"""``extract_with_status`` says why an agent gave no findings (#1088)."""

from __future__ import annotations

import json

import pytest

from ai_pr_review.findings.extract import extract_findings, extract_with_status


def _block(items: list[object]) -> str:
    return "text\n```json-findings\n" + json.dumps(items) + "\n```"


_GOOD = {"severity": "High", "confidence": 90, "finding": "solid", "file": "a.py", "line": 1}


@pytest.mark.parametrize(
    ("output", "truncated", "status", "emitted", "parsed"),
    [
        (_block([]), False, "ok", 0, 0),
        (_block([_GOOD]), False, "ok", 1, 1),
        ("only prose", False, "no_block", None, 0),
        ("", False, "no_block", None, 0),
        ("only prose", True, "truncated_no_block", None, 0),
        ("```json-findings\n[{oops\n```", False, "invalid_json", None, 0),
        ("```json-findings\n[{oops\n```", True, "truncated_invalid", None, 0),
        ('```json-findings\n{"a": 1}\n```', False, "not_array", None, 0),
        ("```json-findings\n  \n```", False, "invalid_json", None, 0),
    ],
)
def test_status_emitted_and_parsed(
    output: str, truncated: bool, status: str, emitted: int | None, parsed: int
) -> None:
    result = extract_with_status(output, "agent", truncated=truncated)
    assert (result.status, result.emitted, len(result.findings)) == (status, emitted, parsed)


def test_truncated_block_that_can_be_repaired_is_not_called_unreadable() -> None:
    """The status and the findings come from one parse, so a salvage shows as
    ``truncated_repaired`` and not as ``invalid_json`` next to ``parsed=1``."""
    one = json.dumps(_GOOD, indent=2)
    cut = "```json-findings\n[\n" + one + ',\n{"severity": "Hi\n```'
    result = extract_with_status(cut, "agent", truncated=True)
    assert result.status == "truncated_repaired"
    assert len(result.findings) == 1
    assert result.emitted == 1


def test_items_dropped_inside_the_parse_show_as_emitted_above_parsed() -> None:
    """Every item rejected while reading must not look like "found nothing"."""
    bad_severity = {"severity": "Nope", "confidence": 90, "finding": "x"}
    refuting = {
        "severity": "High", "confidence": 90, "file": "a.py", "line": 2,
        "finding": "A bug. On closer inspection the logic is correct.",
    }
    result = extract_with_status(_block([bad_severity, "not an object", refuting, _GOOD]), "agent")
    assert result.status == "ok"
    assert result.emitted == 4
    assert len(result.findings) == 1


def test_extract_findings_returns_the_same_findings() -> None:
    out = _block([_GOOD])
    assert extract_findings(out, "agent") == extract_with_status(out, "agent").findings


@pytest.mark.parametrize(
    ("status", "unreadable"),
    [
        ("ok", False),
        ("truncated_repaired", False),
        ("no_block", True),
        ("truncated_no_block", True),
        ("invalid_json", True),
        ("truncated_invalid", True),
        ("not_array", True),
    ],
)
def test_is_unreadable(status: str, unreadable: bool) -> None:
    from ai_pr_review.findings.extract import is_unreadable

    assert is_unreadable(status) is unreadable


@pytest.mark.parametrize(
    "block",
    ["```json-findings\r\n[]\r\n```", "```json-findings \n[]\n```"],
)
def test_fence_tolerates_crlf_and_trailing_space(block: str) -> None:
    assert extract_with_status(block).status == "ok"
