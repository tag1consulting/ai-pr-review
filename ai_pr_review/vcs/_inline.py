"""Provider-agnostic inline-comment payload construction.

Each VCS provider has a different inline-comment JSON shape, but the eligibility
checks (diff-anchor validity, multi-line suggestion range, triple-backtick
rejection) are identical. This module exposes pure helpers each provider
calls with its own renderer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from ai_pr_review.findings.models import Finding

MAX_SUGGESTION_RANGE: Final[int] = 100


def is_inline_eligible(
    finding: Finding, eligible_new: set[tuple[str, int]]
) -> bool:
    """True iff (file, line) is on an added line in the diff."""
    if not finding.file or finding.line is None:
        return False
    return (finding.file, finding.line) in eligible_new


def is_suggestion_safe(finding: Finding) -> bool:
    """Reject suggestions containing triple-backticks (fence-escape attack)."""
    return bool(finding.suggested_code) and "```" not in finding.suggested_code


def is_suggestion_range_valid(
    finding: Finding,
    *,
    eligible_context: set[tuple[str, int]],
) -> bool:
    """True iff start_line..line is a valid multi-line suggestion range.

    Conditions:
    - start_line is set and != line
    - 1 <= start_line <= line
    - range size <= MAX_SUGGESTION_RANGE
    - every line in [start_line, line] is in eligible_context (added or context)
    """
    if (
        finding.line is None
        or finding.start_line is None
        or finding.start_line == finding.line
        or finding.start_line < 1
        or finding.start_line > finding.line
    ):
        return False
    if finding.line - finding.start_line + 1 > MAX_SUGGESTION_RANGE:
        return False
    if not finding.file:
        return False
    return all(
        (finding.file, ln) in eligible_context
        for ln in range(finding.start_line, finding.line + 1)
    )


def partition_findings(
    findings: list[Finding],
    *,
    eligible_new: set[tuple[str, int]],
    max_inline: int,
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (inline, body_only) honoring the inline cap.

    A finding goes inline iff (a) its anchor is in eligible_new AND (b) the
    cap hasn't been hit. Everything else goes body.
    """
    inline: list[Finding] = []
    body: list[Finding] = []
    for f in findings:
        if len(inline) < max_inline and is_inline_eligible(f, eligible_new):
            inline.append(f)
        else:
            body.append(f)
    return inline, body


# Type alias for a provider-supplied inline body renderer.
InlineBodyRenderer = Callable[[Finding], str]
