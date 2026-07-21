"""Tests for ai_pr_review.vcs._body."""

from __future__ import annotations

import itertools

from ai_pr_review.findings.models import Finding
from ai_pr_review.review.outcome import classify_review_outcome
from ai_pr_review.vcs._body import (
    GITHUB_MAX_BODY_SIZE,
    build_agent_prompt,
    compute_headline,
    format_body_finding,
    format_source_tag,
    sanitize_display_text,
    severity_icon,
    truncate_body,
)


def test_sanitize_defangs_details_tags() -> None:
    out = sanitize_display_text("</details><!-- hidden -->next")
    assert "</details>" not in out
    assert "<!--" not in out
    assert "-->" not in out
    assert "details" in out


def test_sanitize_is_case_insensitive_on_tags() -> None:
    assert "<DETAILS>" not in sanitize_display_text("<DETAILS>")
    assert "<Summary>" not in sanitize_display_text("<Summary>")


def test_sanitize_leaves_benign_markdown_intact() -> None:
    text = "Use `os.system` and **bold** and a list:\n- item"
    assert sanitize_display_text(text) == text


def test_sanitize_empty_string() -> None:
    assert sanitize_display_text("") == ""


def test_format_body_finding_defangs_injected_details() -> None:
    # A prompt-injected finding cannot collapse sibling findings via <details>.
    f = Finding(
        severity="High",
        confidence=90,
        finding="real issue</details><!-- hide the rest -->",
        file="a.py",
        line=3,
        remediation="</summary>sneaky",
    )
    out = format_body_finding(f)
    assert "</details>" not in out
    assert "<!--" not in out
    assert "</summary>" not in out


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


# ---------------------------------------------------------------------------
# compute_headline — #622 regression coverage
# ---------------------------------------------------------------------------

_RISK_RANK: dict[str, int] = {
    "None": 0,
    "Unknown": 0,
    "Low": 1,
    "Medium": 2,
    "High": 3,
    "Critical": 4,
}


class _AsFindingLike:
    """Local copy of orchestrate.py's adapter — avoids importing orchestrate
    (which pulls in the full agent-dispatch stack) just for this Protocol
    bridge in a vcs-layer test."""

    __slots__ = ("severity",)

    def __init__(self, f: Finding) -> None:
        self.severity: str = f.severity


def test_compute_headline_excludes_genuine_out_of_diff() -> None:
    """A true out_of_diff finding (apply_diff_scope's meaning: capped to Low)
    is excluded from both risk and count — the collapsed <details> case."""
    ood_low = Finding(
        severity="Low", confidence=80, finding="style nit", source="phpcs",
        out_of_diff=True,
    )
    headline = compute_headline([ood_low], failed_agents=[])
    assert headline.risk == "None"
    assert headline.count == 0


def test_compute_headline_never_excludes_demoted_to_body() -> None:
    """#622: a judge-downranked finding (demoted_to_body=True) must count at
    its true severity — downrank changes placement, never risk."""
    demoted_high = Finding(
        severity="High", confidence=65, finding="weak auth check",
        source="code-reviewer", demoted_to_body=True,
    )
    headline = compute_headline([demoted_high], failed_agents=[])
    assert headline.risk == "High"
    assert headline.count == 1


def test_compute_headline_no_findings_no_failures() -> None:
    headline = compute_headline([], failed_agents=[])
    assert headline.risk == "None"
    assert headline.count == 0


def test_compute_headline_no_findings_with_failures_is_unknown() -> None:
    headline = compute_headline([], failed_agents=["code-reviewer"])
    assert headline.risk == "Unknown"
    assert headline.count == 0


def test_compute_headline_picks_highest_severity() -> None:
    findings = [
        Finding(severity="Low", confidence=80, finding="a"),
        Finding(severity="Medium", confidence=80, finding="b"),
        Finding(severity="Critical", confidence=80, finding="c"),
    ]
    headline = compute_headline(findings, failed_agents=[])
    assert headline.risk == "Critical"
    assert headline.count == 3


def test_compute_headline_never_disagrees_with_classify_review_outcome() -> None:
    """Property test: for every REALISTIC finding-set shape exercised here,
    the headline's risk must never rank LOWER than classify_review_outcome's
    risk for the identical findings. This is the exact invariant #622
    violated (GitHub's old _top_risk() silently dropped a judge-downranked
    High from the headline while classify_review_outcome correctly saw it
    and returned REQUEST_CHANGES) — this test would have caught it.

    "Realistic" means out_of_diff is only ever paired with severity=Low, the
    one combination apply_diff_scope actually produces (see findings/models.py's
    documented invariant and test_model_out_of_diff_implies_low_severity_by_convention
    below for the model-level guard against violating it). Testing out_of_diff
    with a non-Low severity here would assert against a state production code
    is never supposed to construct, which produced a false failure in an
    earlier draft of this test — worth noting so a future editor doesn't
    "fix" this test back into asserting an impossible case.

    Covers combinations of severity x demoted_to_body, plus the
    failed-agents no-findings edge case.
    """
    severities = ("Critical", "High", "Medium", "Low")
    demoted_options = (False, True)

    for r in range(0, 3):
        for combo in itertools.product(
            itertools.product(severities, demoted_options), repeat=r
        ):
            findings = [
                Finding(
                    severity=sev,
                    confidence=80,
                    finding="f",
                    demoted_to_body=demoted,
                )
                for sev, demoted in combo
            ]
            headline = compute_headline(findings, failed_agents=[])
            outcome = classify_review_outcome(
                [_AsFindingLike(f) for f in findings], [], "quick"
            )
            assert _RISK_RANK[headline.risk] >= _RISK_RANK[outcome.risk], (
                f"headline risk {headline.risk!r} ranks below outcome risk "
                f"{outcome.risk!r} for findings={findings!r} — the review "
                "body would understate a risk the actual review state "
                "already reflects, reproducing #622"
            )


def test_model_out_of_diff_implies_low_severity_by_convention() -> None:
    """Guards the invariant compute_headline's exclusion logic depends on:
    the only code path that sets out_of_diff=True (apply_diff_scope) always
    pairs it with severity=Low in the same model_copy. This is a convention,
    not a pydantic-enforced constraint (Finding intentionally has no
    cross-field validator here — see judge.py's comment on why model_copy
    skips validator re-runs) — this test is the guard that would fail if a
    future change sets out_of_diff=True on a non-Low finding, which is
    exactly the shape of bug #622 turned out to be (via demoted_to_body's
    predecessor conflating the two flags)."""
    from ai_pr_review.findings.scope import apply_diff_scope

    diff_text = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " context\n"
        "+added\n"
    )
    # A High finding on a line NOT in the diff — apply_diff_scope must cap it.
    f = Finding(
        severity="High", confidence=90, finding="pre-existing issue",
        source="phpcs", file="app.py", line=999,
    )
    scoped = apply_diff_scope([f], diff_text, mode="cap")
    assert len(scoped) == 1
    if scoped[0].out_of_diff:
        assert scoped[0].severity == "Low", (
            "apply_diff_scope set out_of_diff=True without capping severity "
            "to Low — this breaks compute_headline's exclusion invariant "
            "and would reproduce #622's class of bug"
        )


def test_compute_headline_never_disagrees_with_outcome_when_failed_agents() -> None:
    """Same invariant, but covering the any_failed=True branch of
    classify_review_outcome (which can downgrade APPROVE to COMMENT but
    never changes the risk label itself)."""
    findings = [
        Finding(severity="High", confidence=65, finding="f", demoted_to_body=True),
    ]
    headline = compute_headline(findings, failed_agents=["silent-failure-hunter"])
    outcome = classify_review_outcome(
        [_AsFindingLike(f) for f in findings], ["silent-failure-hunter"], "quick"
    )
    assert _RISK_RANK[headline.risk] >= _RISK_RANK[outcome.risk]
