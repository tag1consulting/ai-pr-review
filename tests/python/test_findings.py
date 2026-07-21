"""Tests for findings pipeline (extract, merge, suppress)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_pr_review.findings.extract import extract_findings
from ai_pr_review.findings.merge import merge_findings
from ai_pr_review.findings.models import Finding

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _make_finding(**kw: object) -> Finding:
    defaults: dict[str, object] = {
        "severity": "High",
        "confidence": 80,
        "finding": "Test finding",
        "source": "test",
        "file": "foo.py",
        "line": 10,
    }
    return Finding.model_validate(defaults | kw)


# ---------------------------------------------------------------------------
# extract_findings
# ---------------------------------------------------------------------------


def test_extract_findings_basic() -> None:
    output = """
Some preamble text.

```json-findings
[
  {
    "severity": "High",
    "confidence": 85,
    "file": "foo.py",
    "line": 42,
    "finding": "SQL injection risk",
    "source": "security-reviewer"
  }
]
```
"""
    findings = extract_findings(output, "security-reviewer")
    assert len(findings) == 1
    assert findings[0].severity == "High"
    assert findings[0].line == 42


def test_extract_findings_stamps_source() -> None:
    output = """
```json-findings
[{"severity": "Low", "confidence": 76, "finding": "style issue"}]
```
"""
    findings = extract_findings(output, "code-reviewer")
    assert findings[0].source == "code-reviewer"


def test_extract_findings_no_block_returns_empty() -> None:
    findings = extract_findings("No findings block here.", "agent")
    assert findings == []


def test_extract_findings_invalid_json_returns_empty() -> None:
    output = "```json-findings\n{bad json\n```\n"
    findings = extract_findings(output, "agent")
    assert findings == []


def test_extract_findings_truncated_no_fence_returns_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """truncated=True with no fence logs the truncation warning and returns []."""
    output = (_FIXTURES / "truncated-no-fence.md").read_text()
    findings = extract_findings(output, "code-reviewer", truncated=True)
    assert findings == []
    captured = capsys.readouterr()
    assert "truncated before json-findings block" in captured.err
    assert "code-reviewer" in captured.err


def test_extract_findings_truncated_json_salvaged(capsys: pytest.CaptureFixture[str]) -> None:
    """truncated=True with a fenced block whose JSON is internally incomplete.

    _try_repair walks backwards to the last complete '}'  and reconstructs the
    array, recovering findings that arrived before truncation.
    """
    # Two complete objects followed by a partial third (no closing brace/bracket).
    # The fence IS closed so _FENCE_RE can match, but json.loads fails on the
    # incomplete array -- triggering _try_repair.
    output = (
        "```json-findings\n"
        '[\n'
        '  {"severity":"High","confidence":85,"file":"foo.py","line":1,"finding":"f1"},\n'
        '  {"severity":"Medium","confidence":77,"file":"bar.py","line":2,"finding":"f2"},\n'
        '  {"severity":"Low","confidence":76,"file":"baz.py","line":3,"finding":"Partial find\n'
        "```\n"
    )
    findings = extract_findings(output, "code-reviewer", truncated=True)
    assert len(findings) >= 2
    assert all(f.confidence >= 75 for f in findings)
    captured = capsys.readouterr()
    assert "salvaged" in captured.err


def test_extract_findings_normalise_severity() -> None:
    output = """
```json-findings
[{"severity": "high", "confidence": 80, "finding": "test"}]
```
"""
    findings = extract_findings(output, "agent")
    assert findings[0].severity == "High"


def test_extract_findings_category_defaults_to_other() -> None:
    """A finding with no category field at all defaults to 'other'."""
    output = """
```json-findings
[{"severity": "High", "confidence": 80, "finding": "test"}]
```
"""
    findings = extract_findings(output, "agent")
    assert findings[0].category == "other"


def test_extract_findings_unknown_category_normalises_to_other() -> None:
    """An unrecognized category value must not raise or drop the finding."""
    output = """
```json-findings
[{"severity": "High", "confidence": 80, "finding": "test", "category": "totally-made-up"}]
```
"""
    findings = extract_findings(output, "agent")
    assert len(findings) == 1
    assert findings[0].category == "other"


def test_extract_findings_valid_category_round_trips() -> None:
    output = """
```json-findings
[{"severity": "High", "confidence": 80, "finding": "test", "category": "secret"}]
```
"""
    findings = extract_findings(output, "agent")
    assert findings[0].category == "secret"
    assert findings[0].to_dict()["category"] == "secret"


@pytest.mark.parametrize("raw_category", [42, None, True, ["secret"], {"a": 1}])
def test_finding_non_string_category_normalises_to_other(raw_category: object) -> None:
    """A non-string category (number, null, bool, list, dict) must fall back to
    'other' rather than raise. This is the same contract as an unrecognized
    *string* value (see test_extract_findings_unknown_category_normalises_to_other),
    but exercised via direct model construction: extract_findings's json.loads()
    parses valid JSON before Finding.model_validate() ever sees it, so an LLM
    emitting a non-string category value is a realistic input this validator
    must handle without raising, else extract.py's broad except-Exception guard
    would silently drop the whole finding (see extract.py's _parse_and_validate)."""
    finding = _make_finding(category=raw_category)
    assert finding.category == "other"


def test_extract_findings_strips_out_of_diff_injection() -> None:
    """out_of_diff: true in agent JSON must be stripped to prevent suppression bypass."""
    output = """
```json-findings
[{"severity": "Critical", "confidence": 90, "finding": "real bug", "out_of_diff": true}]
```
"""
    findings = extract_findings(output, "security-reviewer")
    assert len(findings) == 1
    assert not findings[0].out_of_diff, (
        "out_of_diff injected via agent JSON must be reset to False by extract_findings"
    )
    assert findings[0].severity == "Critical"


def test_extract_findings_strips_demoted_to_body_injection() -> None:
    """demoted_to_body: true in agent JSON must be stripped to prevent suppression
    bypass -- demoted_to_body is out_of_diff's twin (set internally by
    judge._apply_verdicts, not by agents) and is subject to the identical
    injection risk: an LLM agent or prompt-injected diff content could otherwise
    force its own finding out of inline PR visibility into the collapsed review
    body by claiming demoted_to_body=true directly in its JSON output."""
    output = """
```json-findings
[{"severity": "Critical", "confidence": 90, "finding": "real bug", "demoted_to_body": true}]
```
"""
    findings = extract_findings(output, "security-reviewer")
    assert len(findings) == 1
    assert not findings[0].demoted_to_body, (
        "demoted_to_body injected via agent JSON must be reset to False by extract_findings"
    )
    assert findings[0].severity == "Critical"


# ---------------------------------------------------------------------------
# Self-refuting finding lint pass (issue #504)
# ---------------------------------------------------------------------------

# Each parametrize entry is a paraphrase of an actual self-refuting finding
# emitted by the code-reviewer agent on PR #490 (lockfile parsers). All five
# were posted at High severity despite the agent's own reasoning concluding
# the issue was not real. The lint pass must drop them.
@pytest.mark.parametrize(
    "narrative",
    [
        # F1: yarn.lock stanza flush logic
        "yarn.lock stanza flush logic is incorrect... On closer inspection the logic is correct.",
        # F2: _parse_yarn_lock blank-line handling
        "_parse_yarn_lock flushes on blank lines... This is acceptable for malformed input. No actionable bug.",
        # F3: scoped-package name extraction
        "In _parse_yarn_lock, the scoped-package name extraction... No bug — withdraw.",
        # F4: _parse_pnpm_lock_yaml v9 indentation
        "_parse_pnpm_lock_yaml skips lines indented by 3+ spaces... No actual bug here — withdraw.",
        # F5: _parse_gemfile_lock indentation
        "_parse_gemfile_lock only matches gem entries with 4 spaces... No bug.",
    ],
    ids=["F1-on-closer-inspection-correct", "F2-no-actionable-bug",
         "F3-no-bug-withdraw", "F4-no-actual-bug", "F5-no-bug"],
)
def test_extract_findings_drops_self_refuting(
    narrative: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Self-refuting findings (per _governance.md rule 1) are dropped at extract time.

    Reproduces the PR #490 review pattern: agent emits a High finding whose
    own narrative concludes "no bug — withdraw" or equivalent. The lint pass
    in extract_findings must drop these and log a WARNING.
    """
    output = f"""
```json-findings
[{{"severity": "High", "confidence": 85, "file": "foo.py", "line": 1,
   "finding": {json.dumps(narrative)}}}]
```
"""
    findings = extract_findings(output, "code-reviewer")
    assert findings == [], f"self-refuting finding was not dropped: {narrative!r}"
    captured = capsys.readouterr()
    assert "self-refuting" in captured.err
    assert "code-reviewer" in captured.err


def test_extract_findings_keeps_real_findings_that_mention_bug_or_correct() -> None:
    """The lint pass must not false-positive on real findings.

    These narratives genuinely describe bugs or use the word 'correct' in a
    non-refutation context. They must be retained.
    """
    output = """
```json-findings
[
  {"severity": "High", "confidence": 90, "file": "a.py", "line": 1,
   "finding": "This buffer overflow is a serious bug that needs fixing."},
  {"severity": "Medium", "confidence": 80, "file": "b.py", "line": 1,
   "finding": "The current code is incorrect because of an off-by-one error."},
  {"severity": "Low", "confidence": 76, "file": "c.py", "line": 1,
   "finding": "The fix correctly handles the None case, but introduces a leak elsewhere."}
]
```
"""
    findings = extract_findings(output, "code-reviewer")
    assert len(findings) == 3, f"real findings were dropped as self-refuting: {findings}"


def test_extract_findings_drops_self_refuting_in_remediation() -> None:
    """The lint pass scans both `finding` and `remediation` for refutation phrases.

    Some agents put the "actually no bug" conclusion in the remediation field
    rather than the finding text itself.
    """
    output = """
```json-findings
[{"severity": "Medium", "confidence": 85, "file": "x.py", "line": 1,
  "finding": "Possible stale read in cache layer.",
  "remediation": "Re-read: actually this is correct, the cache is invalidated upstream. No action needed."}]
```
"""
    findings = extract_findings(output, "code-reviewer")
    assert findings == [], "self-refuting remediation was not detected"


# ---------------------------------------------------------------------------
# merge_findings / dedup
# ---------------------------------------------------------------------------


def test_merge_filters_by_confidence() -> None:
    findings = [
        _make_finding(confidence=74, line=5),   # below threshold — dropped
        _make_finding(confidence=75, line=50),  # at threshold — kept
        _make_finding(confidence=90, line=100), # above threshold — kept
    ]
    result = merge_findings(findings, confidence_threshold=75)
    assert len(result) == 2
    assert all(f.confidence >= 75 for f in result)


def test_merge_dedup_same_file_nearby_lines() -> None:
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="a.py", line=12, source="agent2"),  # within 3 lines
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert sorted(result[0].sources) == ["agent1", "agent2"]


def test_merge_dedup_preserves_distinct_nearby() -> None:
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="a.py", line=20, source="agent2"),  # 10 lines apart — distinct
    ]
    result = merge_findings(findings)
    assert len(result) == 2


def test_merge_dedup_different_files() -> None:
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="b.py", line=10, source="agent2"),
    ]
    result = merge_findings(findings)
    assert len(result) == 2


def test_merge_severity_order() -> None:
    findings = [
        _make_finding(file="a.py", line=1, severity="Low", confidence=80),
        _make_finding(file="a.py", line=100, severity="Critical", confidence=80),
        _make_finding(file="a.py", line=200, severity="Medium", confidence=80),
    ]
    result = merge_findings(findings)
    assert result[0].severity == "Critical"
    assert result[1].severity == "Medium"
    assert result[2].severity == "Low"


def test_merge_empty_input() -> None:
    assert merge_findings([]) == []


def test_merge_proximity_chaining() -> None:
    """Findings at lines 1, 3, 6 should all merge when PROXIMITY_LINES=3.

    Line 1 and 3 are within 3 (merged). Line 3 and 6 are within 3 (also
    merged). Without tail-comparison, line 6 would incorrectly be compared
    against line 1 (distance 5 > 3) and start a new cluster.
    """
    findings = [
        _make_finding(file="a.py", line=1, source="s1"),
        _make_finding(file="a.py", line=3, source="s2"),
        _make_finding(file="a.py", line=6, source="s3"),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert sorted(result[0].sources) == ["s1", "s2", "s3"]


# ---------------------------------------------------------------------------
# Category-aware dedup (#578)
# ---------------------------------------------------------------------------


def test_merge_dedup_same_line_different_category_not_merged() -> None:
    """Two real, differing categories on nearby lines must not collapse."""
    findings = [
        _make_finding(file="a.py", line=10, source="s1", category="secret"),
        _make_finding(file="a.py", line=11, source="s2", category="injection"),
    ]
    result = merge_findings(findings)
    assert len(result) == 2


def test_merge_dedup_same_line_one_other_category_still_merges() -> None:
    """'other' is a wildcard — it must not block a merge with a real category."""
    findings = [
        _make_finding(file="a.py", line=10, source="s1", category="secret"),
        _make_finding(file="a.py", line=11, source="s2", category="other"),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].category == "secret"


def test_merge_dedup_category_chaining_breaks_on_mismatch() -> None:
    """A real-category mismatch mid-chain splits the cluster there."""
    findings = [
        _make_finding(file="a.py", line=1, source="s1", category="lint"),
        _make_finding(file="a.py", line=3, source="s2", category="secret"),
        _make_finding(file="a.py", line=6, source="s3", category="secret"),
    ]
    result = merge_findings(findings)
    # line 1 (lint) vs line 3 (secret): incompatible, stays separate.
    # line 3 (secret) vs line 6 (secret): compatible, merges.
    assert len(result) == 2
    categories = sorted(f.category for f in result)
    assert categories == ["lint", "secret"]


def test_merge_dedup_other_bridge_does_not_merge_incompatible_reals() -> None:
    """An 'other'-tagged finding must not bridge two incompatible real categories.

    Line 1 (secret) and line 3 (other) are proximity+category compatible with
    each other, and line 3 (other) and line 6 (injection) are too — but line 1
    (secret) and line 6 (injection) are genuinely different findings. Since
    _dedup_file only compares each new finding against the cluster tail, a
    naive implementation lets "other" act as a bridge and silently drops one
    of the two real-category findings in _collapse_cluster.
    """
    findings = [
        _make_finding(file="a.py", line=1, source="s1", category="secret"),
        _make_finding(file="a.py", line=3, source="s2", category="other"),
        _make_finding(file="a.py", line=6, source="s3", category="injection"),
    ]
    result = merge_findings(findings)
    assert len(result) == 2
    categories = sorted(f.category for f in result)
    assert categories == ["injection", "secret"]


def test_merge_collapse_preserves_real_category_from_non_best_member() -> None:
    """The surviving category must come from the real-category member even when
    a different (higher-severity) member is the 'other'-tagged one."""
    findings = [
        _make_finding(
            file="a.py", line=10, source="s1", severity="Low", category="secret"
        ),
        _make_finding(
            file="a.py", line=11, source="s2", severity="Critical", category="other"
        ),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].severity == "Critical"
    assert result[0].category == "secret"


def test_merge_corroboration_preserved_with_matching_real_categories() -> None:
    """Corroboration survives when both sides self-report the SAME real category."""
    findings = [
        _make_finding(
            file="a.py", line=10, source="semgrep", confidence=90, category="injection"
        ),
        _make_finding(
            file="a.py",
            line=12,
            source="security-reviewer",
            confidence=90,
            category="injection",
        ),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is True
    assert result[0].category == "injection"


def test_merge_corroboration_preserved_when_agent_omits_category() -> None:
    """Corroboration survives when one side has a real category and the other
    defaults to 'other' -- the common post-#579 case of an analyzer finding
    (now real-categorized) paired with an LLM agent finding that didn't set
    category at all."""
    findings = [
        _make_finding(
            file="a.py", line=10, source="semgrep", confidence=90, category="injection"
        ),
        _make_finding(
            file="a.py", line=12, source="security-reviewer", confidence=90
        ),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is True
    assert result[0].category == "injection"


# ---------------------------------------------------------------------------
# Finding model validation
# ---------------------------------------------------------------------------


def test_finding_rejects_invalid_severity() -> None:
    with pytest.raises(ValueError):
        Finding(severity="Extreme", confidence=80, finding="x")  # type: ignore[arg-type]


def test_finding_rejects_confidence_out_of_range() -> None:
    with pytest.raises(ValueError):
        Finding(severity="High", confidence=101, finding="x")


# ---------------------------------------------------------------------------
# Provenance weighting integration tests (Story 6-1)
# ---------------------------------------------------------------------------


def test_merge_corroboration_boosts_confidence() -> None:
    """Analyzer + LLM-agent on the same location → corroborated + boosted."""
    findings = [
        _make_finding(file="a.py", line=10, source="semgrep", confidence=90),
        _make_finding(file="a.py", line=12, source="security-reviewer", confidence=90),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is True
    assert result[0].confidence == 100  # 90 + 10


def test_merge_two_agents_no_boost() -> None:
    """Two LLM agents — no analyzer present → no corroboration, confidence unchanged."""
    findings = [
        _make_finding(file="a.py", line=10, source="code-reviewer", confidence=80),
        _make_finding(file="a.py", line=12, source="blind-hunter", confidence=80),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is False
    assert result[0].confidence == 80


def test_merge_two_analyzers_no_boost() -> None:
    """Two analyzers — no LLM agent present → no corroboration."""
    findings = [
        _make_finding(file="a.py", line=10, source="semgrep", confidence=90),
        _make_finding(file="a.py", line=12, source="ruff", confidence=90),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is False


def test_merge_single_finding_no_boost() -> None:
    """A single finding is never corroborated."""
    findings = [_make_finding(source="semgrep", confidence=90)]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is False
    assert result[0].confidence == 90


def test_merge_corroboration_cap() -> None:
    """Boost is capped at 100 — 95 + 10 = 100, not 105."""
    findings = [
        _make_finding(file="a.py", line=10, source="shellcheck", confidence=95),
        _make_finding(file="a.py", line=12, source="security-reviewer", confidence=95),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is True
    assert result[0].confidence == 100


def test_merge_distant_not_corroborated() -> None:
    """Analyzer and agent >3 lines apart → two separate findings, both uncorroborated."""
    findings = [
        _make_finding(file="a.py", line=10, source="semgrep", confidence=90),
        _make_finding(file="a.py", line=50, source="security-reviewer", confidence=90),
    ]
    result = merge_findings(findings)
    assert len(result) == 2
    assert all(f.corroborated is False for f in result)


def test_merge_default_source_not_corroborated() -> None:
    """The 'test' source (the _make_finding default) is neither analyzer nor agent."""
    # Two 'test' sources nearby — they merge but are not corroborated.
    findings = [
        _make_finding(file="a.py", line=10, source="test", confidence=80),
        _make_finding(file="a.py", line=12, source="test", confidence=80),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert result[0].corroborated is False


def test_merge_existing_two_agent_cluster_stays_uncorroborated() -> None:
    """Regression: the existing proximity test with agent1/agent2 stays uncorroborated."""
    findings = [
        _make_finding(file="a.py", line=10, source="agent1"),
        _make_finding(file="a.py", line=12, source="agent2"),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert sorted(result[0].sources) == ["agent1", "agent2"]
    assert result[0].corroborated is False


def test_merge_existing_proximity_chaining_stays_uncorroborated() -> None:
    """Regression: three-way chain of unknown sources stays uncorroborated."""
    findings = [
        _make_finding(file="a.py", line=1, source="s1"),
        _make_finding(file="a.py", line=3, source="s2"),
        _make_finding(file="a.py", line=6, source="s3"),
    ]
    result = merge_findings(findings)
    assert len(result) == 1
    assert sorted(result[0].sources) == ["s1", "s2", "s3"]
    assert result[0].corroborated is False


def test_finding_to_dict() -> None:
    f = Finding(
        severity="High",
        confidence=80,
        finding="test",
        source="s",
        file="f.py",
        line=5,
    )
    d = f.to_dict()
    assert d["severity"] == "High"
    assert d["file"] == "f.py"
    assert d["line"] == 5
    assert d["category"] == "other"
