"""Tests for ai_pr_review.vcs._inline shared eligibility helpers."""

from __future__ import annotations

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._inline import (
    MAX_SUGGESTION_RANGE,
    is_inline_eligible,
    is_suggestion_range_valid,
    is_suggestion_safe,
    partition_findings,
    split_body_findings,
)


def _f(**kwargs: object) -> Finding:
    base = {"severity": "Low", "confidence": 50, "finding": "x"}
    base.update(kwargs)
    return Finding(**base)  # type: ignore[arg-type]


def test_is_inline_eligible_true_when_in_set() -> None:
    assert is_inline_eligible(_f(file="a.py", line=3), {("a.py", 3)}) is True


def test_is_inline_eligible_false_when_missing() -> None:
    assert is_inline_eligible(_f(file="a.py", line=3), {("a.py", 4)}) is False


def test_is_inline_eligible_no_file_or_line() -> None:
    assert is_inline_eligible(_f(), set()) is False
    assert is_inline_eligible(_f(file="a.py"), {("a.py", 1)}) is False


def test_is_suggestion_safe_true_when_no_backticks() -> None:
    assert is_suggestion_safe(_f(suggested_code="x = 1\ny = 2")) is True


def test_is_suggestion_safe_false_with_triple_backticks() -> None:
    assert is_suggestion_safe(_f(suggested_code="```\nbad")) is False


def test_is_suggestion_safe_false_when_empty() -> None:
    assert is_suggestion_safe(_f()) is False


def test_is_suggestion_range_valid_happy() -> None:
    f = _f(file="a.py", line=10, start_line=8)
    ctx = {("a.py", 8), ("a.py", 9), ("a.py", 10)}
    assert is_suggestion_range_valid(f, eligible_context=ctx) is True


def test_is_suggestion_range_valid_missing_line_in_context() -> None:
    f = _f(file="a.py", line=10, start_line=8)
    ctx = {("a.py", 8), ("a.py", 10)}  # line 9 missing
    assert is_suggestion_range_valid(f, eligible_context=ctx) is False


def test_is_suggestion_range_valid_no_start_line() -> None:
    assert (
        is_suggestion_range_valid(
            _f(file="a.py", line=5), eligible_context={("a.py", 5)}
        )
        is False
    )


def test_is_suggestion_range_valid_start_equal_to_line() -> None:
    # Single-line suggestion uses the regular `line` field — multi-line range
    # must have start_line != line.
    f = _f(file="a.py", line=5, start_line=5)
    assert is_suggestion_range_valid(f, eligible_context={("a.py", 5)}) is False


def test_is_suggestion_range_valid_start_after_line() -> None:
    f = _f(file="a.py", line=5, start_line=10)
    assert is_suggestion_range_valid(f, eligible_context=set()) is False


def test_is_suggestion_range_valid_too_long() -> None:
    f = _f(file="a.py", line=200, start_line=1)
    # 200 lines > MAX_SUGGESTION_RANGE
    assert MAX_SUGGESTION_RANGE < 200
    ctx = {("a.py", n) for n in range(1, 201)}
    assert is_suggestion_range_valid(f, eligible_context=ctx) is False


def test_partition_findings_splits_correctly() -> None:
    findings = [
        _f(file="a.py", line=1),
        _f(file="a.py", line=2),
        _f(file="a.py", line=99),  # ineligible
        _f(file="a.py", line=3),
    ]
    eligible = {("a.py", 1), ("a.py", 2), ("a.py", 3)}
    inline, body = partition_findings(findings, eligible_new=eligible, max_inline=10)
    assert len(inline) == 3
    assert len(body) == 1
    assert body[0].line == 99


def test_partition_findings_respects_cap() -> None:
    findings = [_f(file="a.py", line=i) for i in range(1, 6)]
    eligible = {("a.py", n) for n in range(1, 6)}
    inline, body = partition_findings(findings, eligible_new=eligible, max_inline=2)
    assert len(inline) == 2
    assert len(body) == 3


def test_split_body_findings_separates_ood() -> None:
    findings = [
        _f(file="a.py", line=1, out_of_diff=False),
        _f(file="a.py", line=2, out_of_diff=True),
        _f(file="a.py", line=3, out_of_diff=False),
        _f(file="a.py", line=4, out_of_diff=True),
    ]
    in_diff, ood = split_body_findings(findings)
    assert len(in_diff) == 2
    assert len(ood) == 2
    assert all(not f.out_of_diff for f in in_diff)
    assert all(f.out_of_diff for f in ood)


def test_split_body_findings_all_in_diff() -> None:
    findings = [_f(file="a.py", line=i, out_of_diff=False) for i in range(1, 4)]
    in_diff, ood = split_body_findings(findings)
    assert len(in_diff) == 3
    assert ood == []


def test_split_body_findings_all_ood() -> None:
    findings = [_f(file="a.py", line=i, out_of_diff=True) for i in range(1, 4)]
    in_diff, ood = split_body_findings(findings)
    assert in_diff == []
    assert len(ood) == 3


def test_split_body_findings_empty() -> None:
    in_diff, ood = split_body_findings([])
    assert in_diff == []
    assert ood == []
