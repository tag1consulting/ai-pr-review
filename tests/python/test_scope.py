"""Tests for ai_pr_review.findings.scope."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.scope import (
    ROLLUP_THRESHOLD,
    _ANALYZER_PREFIXES,
    _is_analyzer,
    apply_diff_scope,
    rollup_repeated_findings,
)

# Minimal unified diff: one hunk touching lines 10-11 of web/quickbooks.inc
_DIFF = """\
diff --git a/web/quickbooks.inc b/web/quickbooks.inc
index abc..def 100644
--- a/web/quickbooks.inc
+++ b/web/quickbooks.inc
@@ -9,3 +9,4 @@
 context line
+added line one
+added line two
 context line
"""

# Lines 10 and 11 are in the diff; line 100 is not.


def _phpcs(file: str, line: int, severity: str = "High", text: str = "phpcs finding") -> Finding:
    return Finding(severity=severity, confidence=80, finding=text, source="phpcs", file=file, line=line)


def _phpstan(file: str, line: int, text: str = "phpstan finding") -> Finding:
    return Finding(severity="High", confidence=80, finding=text, source="phpstan", file=file, line=line)


def _agent(file: str, line: int, text: str = "agent finding") -> Finding:
    return Finding(severity="High", confidence=80, finding=text, source="code-reviewer", file=file, line=line)


# apply_diff_scope


def test_in_diff_analyzer_finding_unchanged() -> None:
    f = _phpcs("web/quickbooks.inc", 10)
    result = apply_diff_scope([f], _DIFF)
    assert len(result) == 1
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_out_of_diff_analyzer_finding_capped_to_low() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="cap")
    assert len(result) == 1
    assert result[0].severity == "Low"
    assert result[0].out_of_diff


def test_out_of_diff_analyzer_finding_dropped() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="drop")
    assert result == []


def test_out_of_diff_analyzer_finding_passthrough_when_off() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="off")
    assert len(result) == 1
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_llm_agent_finding_never_capped() -> None:
    f = _agent("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="cap")
    assert len(result) == 1
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_phpstan_out_of_diff_capped() -> None:
    f = _phpstan("web/quickbooks.inc", 500)
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "Low"
    assert result[0].out_of_diff


def test_finding_without_line_not_capped() -> None:
    f = Finding(severity="High", confidence=80, finding="body finding", source="phpcs", file="web/quickbooks.inc")
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_finding_without_file_not_capped() -> None:
    f = Finding(severity="High", confidence=80, finding="global finding", source="phpcs")
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "High"


def test_mixed_findings_correctly_partitioned() -> None:
    findings = [
        _phpcs("web/quickbooks.inc", 10),   # in diff
        _phpcs("web/quickbooks.inc", 100),  # out of diff
        _agent("web/quickbooks.inc", 100),  # LLM, should not be capped
    ]
    result = apply_diff_scope(findings, _DIFF)

    phpcs_findings = [f for f in result if f.source == "phpcs"]
    agent_findings = [f for f in result if f.source == "code-reviewer"]

    # Two phpcs findings: one in-diff (High, out_of_diff=False), one out (Low, out_of_diff=True)
    assert len(phpcs_findings) == 2
    in_diff_phpcs = [f for f in phpcs_findings if not f.out_of_diff]
    ood_phpcs = [f for f in phpcs_findings if f.out_of_diff]
    assert len(in_diff_phpcs) == 1 and in_diff_phpcs[0].severity == "High"
    assert len(ood_phpcs) == 1 and ood_phpcs[0].severity == "Low"

    # LLM finding untouched regardless of line position
    assert len(agent_findings) == 1
    assert agent_findings[0].severity == "High"
    assert not agent_findings[0].out_of_diff


def test_multiple_phpcs_in_and_out() -> None:
    findings = [
        _phpcs("web/quickbooks.inc", 10),   # in diff
        _phpcs("web/quickbooks.inc", 11),   # in diff
        _phpcs("web/quickbooks.inc", 50),   # out of diff
        _phpcs("web/quickbooks.inc", 100),  # out of diff
    ]
    result = apply_diff_scope(findings, _DIFF)
    in_diff = [f for f in result if not f.out_of_diff]
    out_of_diff = [f for f in result if f.out_of_diff]
    assert len(in_diff) == 2
    assert len(out_of_diff) == 2
    assert all(f.severity == "High" for f in in_diff)
    assert all(f.severity == "Low" for f in out_of_diff)


def test_empty_diff_text_passthrough() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], "", mode="cap")
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


# rollup_repeated_findings


def _make_repeated(n: int, file: str = "web/file.php", source: str = "phpcs", text: str = "Use short array syntax") -> list[Finding]:
    return [
        Finding(severity="High", confidence=80, finding=text, source=source, file=file, line=i + 1)
        for i in range(n)
    ]


def test_below_threshold_not_rolled_up() -> None:
    findings = _make_repeated(ROLLUP_THRESHOLD)
    result = rollup_repeated_findings(findings, threshold=ROLLUP_THRESHOLD)
    assert len(result) == ROLLUP_THRESHOLD


def test_above_threshold_rolled_up() -> None:
    findings = _make_repeated(ROLLUP_THRESHOLD + 1)
    result = rollup_repeated_findings(findings, threshold=ROLLUP_THRESHOLD)
    assert len(result) == 1
    assert f"({ROLLUP_THRESHOLD + 1} occurrences" in result[0].finding


def test_rollup_preserves_line_list() -> None:
    findings = _make_repeated(10)
    result = rollup_repeated_findings(findings, threshold=5)
    assert len(result) == 1
    text = result[0].finding
    assert "lines:" in text
    assert "1" in text


def test_different_rules_not_merged() -> None:
    f1 = _make_repeated(10, text="Rule A")
    f2 = _make_repeated(10, text="Rule B")
    result = rollup_repeated_findings(f1 + f2, threshold=5)
    assert len(result) == 2


def test_different_files_not_merged() -> None:
    f1 = _make_repeated(10, file="a.php")
    f2 = _make_repeated(10, file="b.php")
    result = rollup_repeated_findings(f1 + f2, threshold=5)
    assert len(result) == 2


def test_different_sources_not_merged() -> None:
    f1 = _make_repeated(10, source="phpcs")
    f2 = _make_repeated(10, source="phpstan")
    result = rollup_repeated_findings(f1 + f2, threshold=5)
    assert len(result) == 2


def test_non_analyzer_finding_passthrough() -> None:
    agent_findings = [
        _agent("web/file.php", i + 1, text="security issue")
        for i in range(20)
    ]
    result = rollup_repeated_findings(agent_findings, threshold=5)
    assert len(result) == 20


def test_rollup_line_preview_truncated_for_large_sets() -> None:
    findings = _make_repeated(30)
    result = rollup_repeated_findings(findings, threshold=5)
    assert len(result) == 1
    text = result[0].finding
    assert "more" in text


# Config validation


def test_config_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig
    for val in ("cap", "drop", "off"):
        monkeypatch.setenv("AI_ANALYZER_DIFF_SCOPE", val)
        cfg = ReviewConfig.from_env()
        assert cfg.analyzer_diff_scope == val


def test_config_invalid_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig
    monkeypatch.setenv("AI_ANALYZER_DIFF_SCOPE", "invalid")
    with pytest.raises(ValidationError):
        ReviewConfig.from_env()


def test_config_default_is_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig
    monkeypatch.delenv("AI_ANALYZER_DIFF_SCOPE", raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.analyzer_diff_scope == "cap"


# Analyzer prefix coverage — every source emitted by analyzers/run-*.sh must
# be recognised.  If a new analyzer is added, update _ANALYZER_PREFIXES too.


@pytest.mark.parametrize("source", [
    "checkov",
    "eslint",
    "golangci-lint",
    "hadolint",
    "kube-linter",
    "osv",
    "phpcs",
    "phpstan",
    "ruff",
    "sarif:bandit",
    "semgrep",
    "shellcheck",
    "tflint",
    "trufflehog",
])
def test_is_analyzer_recognises_all_sources(source: str) -> None:
    f = Finding(severity="High", confidence=80, finding="x", source=source)
    assert _is_analyzer(f), f"_is_analyzer returned False for source={source!r}"


def test_is_analyzer_rejects_llm_agent_sources() -> None:
    for source in ("code-reviewer", "security-reviewer", "architecture-reviewer", "blind-hunter"):
        f = Finding(severity="High", confidence=80, finding="x", source=source)
        assert not _is_analyzer(f), f"_is_analyzer returned True for LLM agent source={source!r}"


def test_prefix_list_has_no_duplicates() -> None:
    assert len(_ANALYZER_PREFIXES) == len(set(_ANALYZER_PREFIXES)), (
        "Duplicate entries in _ANALYZER_PREFIXES"
    )


# Rollup: in-diff and out-of-diff occurrences of the same rule must not merge.


def test_rollup_does_not_merge_across_diff_boundary() -> None:
    """An in-diff High finding must not be folded into an out-of-diff Low group."""
    # 5 out-of-diff Low findings + 1 in-diff High = 6 total, above threshold.
    # The in-diff High must survive as a separate entry.
    ood = [
        Finding(
            severity="Low", confidence=80, finding="same rule",
            source="phpcs", file="a.php", line=i + 50,
            out_of_diff=True,
        )
        for i in range(5)
    ]
    in_diff = Finding(
        severity="High", confidence=90, finding="same rule",
        source="phpcs", file="a.php", line=5,
        out_of_diff=False,
    )
    result = rollup_repeated_findings(ood + [in_diff], threshold=5)
    # Out-of-diff group (5 entries, exactly at threshold) is preserved unchanged.
    # In-diff entry is kept separate.
    in_diff_results = [f for f in result if not f.out_of_diff]
    ood_results = [f for f in result if f.out_of_diff]
    assert len(in_diff_results) == 1, "in-diff High finding must not be collapsed"
    assert in_diff_results[0].severity == "High"
    assert len(ood_results) == 5
