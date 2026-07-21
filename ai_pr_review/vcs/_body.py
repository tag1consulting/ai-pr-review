"""Body-formatting helpers shared across VCS providers.

Ports severity_icon, classify_risk (display-only), format_source_tag,
format_body_finding, truncate_body, build_agent_prompt from vcs/common.sh.
The review-outcome classification proper lives in
`ai_pr_review.review.outcome` (E2.S6); this module only formats for display.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ai_pr_review.findings.models import Finding

# GitHub's body size limit (bytes). GitLab/Bitbucket have similar but slightly
# different limits — each provider can override.
GITHUB_MAX_BODY_SIZE: Final[int] = 65_536

_SEVERITY_ICONS: Final[dict[str, str]] = {
    "critical": "🚨",
    "high": "🔴",
    "medium": "🟡",
    "low": "🔵",
    "none": "✅",
    "unknown": "❔",
}


def severity_icon(severity: str) -> str:
    return _SEVERITY_ICONS.get(severity.lower(), "🔵")


@dataclass(frozen=True)
class Headline:
    """Review-body headline: the "Overall Risk | Findings: N" line's inputs.

    Shared by every VCS renderer so "Overall Risk" can never disagree with
    itself across providers, and can never contradict the true severity of a
    finding just because that finding was judge-downranked or diff-scoped.
    Fixes tag1consulting/ai-pr-review#622 (GitHub silently excluded
    judge-downranked findings from this calculation via a stale out_of_diff
    filter; Bitbucket over-counted the opposite way by not filtering
    analyzer out-of-diff findings at all).
    """

    risk: str
    count: int


def compute_headline(
    findings: Sequence[Finding], failed_agents: Sequence[str]
) -> Headline:
    """Compute the headline risk label and count for a review body.

    Excludes only genuine ``out_of_diff`` findings (native-analyzer findings
    outside the changed-line set, always capped to Low severity by
    ``apply_diff_scope`` — see ``findings/models.py``'s field docs for the
    invariant). Judge-downranked findings (``demoted_to_body=True``) are
    NEVER excluded here regardless of severity: downrank changes where a
    finding is rendered (inline vs. body), not whether it counts toward risk.

    Note the actual, narrower invariant with ``review.outcome
    .classify_review_outcome`` (which computes the real REQUEST_CHANGES
    /APPROVE decision, with no ``out_of_diff`` filtering of its own): this
    function's risk never ranks *below* that decision's risk for the same
    findings — never a silent understatement. The two functions do NOT
    always agree outright, since ``classify_review_outcome`` counts every
    finding (including ``out_of_diff``) while this function excludes
    ``out_of_diff`` findings from the headline by design; for an
    out_of_diff-only, all-Low finding set the two diverge (headline: None/0;
    outcome: Low/APPROVE) without violating the never-understate invariant,
    since neither one requests changes in that case. A combinatorial test in
    tests/python/vcs/test_body.py (test_compute_headline_never_disagrees
    _with_classify_review_outcome) covers the ``demoted_to_body`` axis this
    invariant actually depends on; it deliberately excludes the
    out_of_diff-mismatched-severity axis, since that state is never
    constructed by production code (see the model-level guard test
    alongside it).
    """
    in_headline = [f for f in findings if not f.out_of_diff]
    count = len(in_headline)

    if count == 0:
        risk = "None" if not failed_agents else "Unknown"
    else:
        risk = "Low"
        for level in ("Critical", "High", "Medium", "Low"):
            if any(f.severity == level for f in in_headline):
                risk = level
                break

    return Headline(risk=risk, count=count)


# HTML control sequences that, when smuggled into a finding via prompt
# injection, would break out of the rendered review structure: <details>/
# <summary> tags can collapse and hide sibling findings from a human reviewer,
# and HTML comment markers can comment out following content. The VCS markdown
# renderers strip dangerous HTML (scripts/handlers), so this is an integrity
# concern, not XSS — but for a security-review tool, hiding flagged findings is
# itself a problem. We defang only these structural sequences (leaving benign
# markdown like code spans, lists, and emphasis intact) by inserting a
# zero-width space after the opening angle-bracket / first dash.
_DEFANG_SEQUENCES: Final[tuple[tuple[str, str], ...]] = (
    ("<details", "<​details"),
    ("</details", "<​/details"),
    ("<summary", "<​summary"),
    ("</summary", "<​/summary"),
    ("<!--", "<​!--"),
    ("-->", "--​>"),
)


def sanitize_display_text(text: str) -> str:
    """Neutralize structure-breaking HTML in LLM-derived display text.

    Applied to ``finding`` and ``remediation`` strings (which can be steered by
    prompt injection in PR content) before they are interpolated into a posted
    comment body. Case-insensitive on the tag sequences.
    """
    if not text:
        return text
    for needle, replacement in _DEFANG_SEQUENCES:
        if needle.startswith("<") and needle != "<!--":
            # Case-insensitive replace for HTML tags (e.g. <DETAILS>, <Details>).
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
            text = pattern.sub(replacement, text)
        else:
            text = text.replace(needle, replacement)
    return text


def format_source_tag(finding: Finding) -> str:
    """Render `[agent1, agent2]` tag from a finding's sources or source field."""
    if finding.sources:
        return f"[{', '.join(finding.sources)}]"
    if finding.source:
        return f"[{finding.source}]"
    return ""


def format_body_finding(
    finding: Finding,
    *,
    location_note: str = "",
    include_suggestion: bool = False,
    finding_id: int | None = None,
) -> str:
    """Render a finding as a single Markdown bullet for the review body.

    Parameters
    ----------
    finding_id:
        Optional stable per-PR numeric ID (e.g. 1 → ``**[F1]**``).  When
        provided, the ID token is inserted between the severity and source
        tags so users can reference it in ``/ai-pr-review dismiss F1``.
    """
    icon = severity_icon(finding.severity)
    source_tag = format_source_tag(finding)
    location = ""
    if finding.file:
        loc_parts = [finding.file]
        if finding.line is not None:
            loc_parts.append(str(finding.line))
        location = ":".join(loc_parts)
    header_parts = [icon, f"**[{finding.severity}]**"]
    if finding_id is not None:
        header_parts.append(f"**[F{finding_id}]**")
    if source_tag:
        header_parts.append(source_tag)
    header_parts.append(sanitize_display_text(finding.finding))
    out = "- " + " ".join(header_parts)
    if location:
        out += f" *(at `{location}`{location_note})*"
    if finding.remediation:
        out += f"\n  - **Remediation:** {sanitize_display_text(finding.remediation)}"
    if include_suggestion and finding.suggested_code:
        fence_body = finding.suggested_code.replace("```", "``​`")
        out += f"\n  ```\n  {fence_body}\n  ```"
    return out


def truncate_body(body: str, limit: int = GITHUB_MAX_BODY_SIZE) -> str:
    """Truncate body at byte boundary, append marker. UTF-8 safe.

    Mirrors the bash `truncate_body`: cuts at `limit` bytes, then drops trailing
    partial UTF-8 by decoding with errors='ignore'.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= limit:
        return body
    head = encoded[:limit].decode("utf-8", errors="ignore")
    trailer = (
        "\n\n---\n"
        "*Review output truncated — body exceeded provider API limit "
        f"({limit:,} bytes). Run a full review locally to see complete output.*"
    )
    return head + trailer


def build_agent_prompt(findings: Sequence[Finding]) -> str:
    """Render the collapsible "Prompt for AI agents" block from findings.

    Ports build_agent_prompt from vcs/common.sh. Placed at the end of the
    review body so users can copy-paste into an AI tool to remediate.
    """
    if not findings:
        return ""
    items = []
    for f in findings:
        location = ""
        if f.file:
            location = f.file
            if f.line is not None:
                location = f"{f.file}:{f.line}"
        entry: dict[str, object] = {
            "severity": f.severity,
            "finding": f.finding,
        }
        if location:
            entry["location"] = location
        if f.remediation:
            entry["remediation"] = f.remediation
        items.append(entry)
    payload = json.dumps(items, indent=2)
    return (
        "<details>\n"
        "<summary>🤖 Prompt for AI agents</summary>\n\n"
        "Copy the JSON below into an AI coding assistant to triage:\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n"
        "</details>"
    )


def join_findings(items: Iterable[str]) -> str:
    """Join body-finding bullets with a blank line between them."""
    return "\n".join(items)
