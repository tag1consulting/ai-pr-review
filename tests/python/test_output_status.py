"""``output_status`` names why an agent gave no findings (#1088)."""

from __future__ import annotations

import pytest

from ai_pr_review.findings.extract import output_status


@pytest.mark.parametrize(
    ("output", "truncated", "expected"),
    [
        ("text\n```json-findings\n[]\n```", False, "ok"),
        ('```json-findings\n[{"a": 1}]\n```', False, "ok"),
        ("only prose", False, "no_block"),
        ("only prose", True, "truncated_no_block"),
        ("```json-findings\n[{oops\n```", False, "invalid_json"),
        ('```json-findings\n{"a": 1}\n```', False, "not_array"),
        ("", False, "no_block"),
    ],
)
def test_output_status(output: str, truncated: bool, expected: str) -> None:
    assert output_status(output, truncated=truncated) == expected
