"""Unit tests for ai_pr_review.findings.provenance.

Tests cover:
- is_corroborated: positive (analyzer+agent), negative (agents-only,
  analyzers-only, empty, unknown, dependency-check not an agent)
- boosted_confidence: normal boost, cap at 100
- Parametrized guard: every _ANALYZER_PREFIXES entry + "code-reviewer" → True
"""

from __future__ import annotations

import pytest

from ai_pr_review.findings.provenance import PROVENANCE_BOOST, boosted_confidence, is_corroborated
from ai_pr_review.findings.scope import _ANALYZER_PREFIXES

# ---------------------------------------------------------------------------
# is_corroborated — positive cases
# ---------------------------------------------------------------------------


def test_corroborated_semgrep_and_security_reviewer() -> None:
    assert is_corroborated(["semgrep", "security-reviewer"]) is True


def test_corroborated_sarif_and_security_reviewer() -> None:
    assert is_corroborated(["sarif:bandit", "security-reviewer"]) is True


def test_corroborated_case_insensitive_analyzer() -> None:
    # Analyzer prefix matching must be case-insensitive.
    assert is_corroborated(["SEMGREP", "code-reviewer"]) is True


def test_corroborated_osv_and_security_reviewer() -> None:
    # osv is the source tag for cve-check; counts as an analyzer.
    assert is_corroborated(["osv", "security-reviewer"]) is True


def test_corroborated_shellcheck_and_blind_hunter() -> None:
    assert is_corroborated(["shellcheck", "blind-hunter"]) is True


def test_corroborated_ruff_and_adversarial_general() -> None:
    assert is_corroborated(["ruff", "adversarial-general"]) is True


# ---------------------------------------------------------------------------
# is_corroborated — negative cases
# ---------------------------------------------------------------------------


def test_not_corroborated_empty() -> None:
    assert is_corroborated([]) is False


def test_not_corroborated_agents_only() -> None:
    assert is_corroborated(["code-reviewer", "blind-hunter"]) is False


def test_not_corroborated_analyzers_only() -> None:
    assert is_corroborated(["semgrep", "ruff"]) is False


def test_not_corroborated_single_analyzer() -> None:
    assert is_corroborated(["osv"]) is False


def test_not_corroborated_dependency_check_not_an_agent() -> None:
    # cve-check emits source="osv" and agent="dependency-check".
    # "dependency-check" is NOT in AGENT_NAMES — it is on the `agent` field,
    # not the `sources` list.  A cve-check finding alone is analyzer-only.
    assert is_corroborated(["osv", "dependency-check"]) is False


def test_not_corroborated_unknown_and_analyzer() -> None:
    # "unknown" is neither an analyzer prefix nor an agent name.
    assert is_corroborated(["unknown", "ruff"]) is False


def test_not_corroborated_unknown_and_agent() -> None:
    assert is_corroborated(["unknown", "code-reviewer"]) is False


def test_not_corroborated_single_agent() -> None:
    assert is_corroborated(["security-reviewer"]) is False


# ---------------------------------------------------------------------------
# boosted_confidence
# ---------------------------------------------------------------------------


def test_boosted_confidence_normal() -> None:
    assert boosted_confidence(80) == 80 + PROVENANCE_BOOST


def test_boosted_confidence_cap_from_95() -> None:
    assert boosted_confidence(95) == 100


def test_boosted_confidence_already_at_100() -> None:
    assert boosted_confidence(100) == 100


def test_boosted_confidence_low_value() -> None:
    assert boosted_confidence(0) == PROVENANCE_BOOST


# ---------------------------------------------------------------------------
# Parametrized guard: every _ANALYZER_PREFIXES entry must trigger corroboration
# when paired with "code-reviewer".  This catches future prefix additions that
# miss the is_analyzer_source helper.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", list(_ANALYZER_PREFIXES))
def test_all_analyzer_prefixes_trigger_corroboration(prefix: str) -> None:
    """Every declared analyzer prefix must result in corroboration when an
    LLM agent source is also present."""
    # Use the prefix itself as the source string (e.g. "sarif:" needs a suffix
    # to be valid — append a dummy tool name).
    source = prefix + "tool" if prefix.endswith(":") else prefix
    assert is_corroborated([source, "code-reviewer"]) is True, (
        f"Prefix {prefix!r} (source={source!r}) did not trigger corroboration"
    )
