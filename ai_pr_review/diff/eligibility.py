"""Inline comment eligibility — mirrors vcs/common.sh added-lines-only logic."""

from __future__ import annotations

from ai_pr_review.diff.linemap import LineRef, parse_added_lines


def eligible_inline_lines(diff_text: str) -> set[LineRef]:
    """Return the set of (file, line) positions eligible for inline comments.

    Only ADDED lines (+) are eligible anchors. This mirrors the bash
    parse_valid_lines behaviour: deleted context lines are not valid
    inline-comment targets on any supported VCS provider.
    """
    return parse_added_lines(diff_text)


def is_eligible(diff_text: str, file: str, line: int) -> bool:
    """Return True if (file, line) is a valid inline comment target."""
    eligible = eligible_inline_lines(diff_text)
    return LineRef(file, line) in eligible
