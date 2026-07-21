"""Offline unit tests for the consistency-eval harness's pure clustering logic.

The harness itself (tests/canary/consistency_eval.py) makes real, billed API
calls and is excluded from the default pytest run -- but its finding-clustering
math (which decides whether two runs "agree") is pure and must be correct for
the reported stability score to mean anything. These tests exercise that math
with synthetic findings and zero network access, so a regression in the
fingerprinting/clustering logic is caught in normal CI even though the live
harness is not run here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from ai_pr_review.findings.models import Category, Finding, Severity

# The harness lives under tests/canary/, which is not an importable package
# (no __init__.py, excluded from the pytest rootdir's import roots). Load it by
# path so these unit tests can reach its functions without making the canary
# directory a package or putting billed-call code on the default import path.
# Register it in sys.modules before exec so its @dataclass definitions resolve
# their own module (dataclasses looks the class's __module__ up in sys.modules).
_HARNESS_PATH = (
    Path(__file__).resolve().parent.parent / "canary" / "consistency_eval.py"
)
_spec = importlib.util.spec_from_file_location("consistency_eval", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
ce = importlib.util.module_from_spec(_spec)
sys.modules["consistency_eval"] = ce
_spec.loader.exec_module(ce)

T = ce.DEFAULT_JACCARD


def _f(severity: Severity, confidence: int, text: str, *, file: str = "x.yml",
       line: int = 100, category: Category = "authz") -> Finding:
    return Finding(
        severity=severity, confidence=confidence, finding=text,
        file=file, line=line, category=category,
    )


def test_paraphrases_of_same_finding_cluster_across_runs() -> None:
    """Three runs describe one issue in different words and at slightly
    different lines; all three must fold into a single cluster seen in every
    run. This is the case that makes the stability score meaningful -- without
    it, every paraphrase would look like a flip-flop."""
    clusters: list[Any] = []
    ce._assign_to_clusters(clusters, 0, [
        _f("High", 80, "author_association is not a reliable authorization check for push access"),
    ], T)
    ce._assign_to_clusters(clusters, 1, [
        _f("High", 62, "The author_association check does not reliably verify current push access", line=103),
    ], T)
    ce._assign_to_clusters(clusters, 2, [
        _f("High", 71, "author_association authorization check fails to verify push access reliably", line=98),
    ], T)
    assert len(clusters) == 1
    assert clusters[0].runs_seen == {0, 1, 2}


def test_different_findings_in_same_file_stay_separate() -> None:
    """Two unrelated findings in the same file, same run, must not collapse:
    the coarse key matches but their keyword overlap is ~0."""
    clusters: list[Any] = []
    ce._assign_to_clusters(clusters, 0, [
        _f("High", 80, "author_association authorization check unreliable for push access"),
        _f("High", 80, "unrelated token scope leakage into downstream job environment", line=101),
    ], T)
    assert len(clusters) == 2


def test_flip_flopper_seen_in_subset_of_runs() -> None:
    """A finding present in 2 of 3 runs records exactly those run indices, so
    the report can distinguish it from a stable finding (seen in all runs)."""
    clusters: list[Any] = []
    ce._assign_to_clusters(clusters, 0, [
        _f("Medium", 60, "missing timeout on the outbound http request call", category="edge-case"),
    ], T)
    ce._assign_to_clusters(clusters, 1, [
        _f("Medium", 58, "the outbound http request call is missing a timeout", line=102, category="edge-case"),
    ], T)
    # run 2 does not report it
    assert len(clusters) == 1
    assert clusters[0].runs_seen == {0, 1}


def test_same_text_different_severity_are_distinct() -> None:
    """Severity is part of the coarse key, so identical prose at two severities
    is two findings -- a High vs Medium disagreement is real instability, not a
    match."""
    clusters: list[Any] = []
    ce._assign_to_clusters(clusters, 0, [
        _f("High", 80, "identical text here about the same underlying thing"),
    ], T)
    ce._assign_to_clusters(clusters, 1, [
        _f("Medium", 80, "identical text here about the same underlying thing"),
    ], T)
    assert len(clusters) == 2


def test_two_matching_findings_in_one_run_count_run_once() -> None:
    """If a single run emits two findings that both match one cluster, that run
    is counted once for the cluster (runs_seen is a set), not twice -- otherwise
    a run could inflate a cluster's apparent cross-run presence."""
    clusters: list[Any] = []
    ce._assign_to_clusters(clusters, 0, [
        _f("High", 80, "author association authorization check unreliable push access"),
        _f("High", 75, "author association check unreliable for push access verify", line=104),
    ], T)
    assert len(clusters) == 1
    assert clusters[0].runs_seen == {0}


def test_different_file_stays_separate_even_with_identical_text() -> None:
    """File is part of the coarse key: the same issue text in two files is two
    findings."""
    clusters: list[Any] = []
    ce._assign_to_clusters(clusters, 0, [
        _f("High", 80, "same finding text about a problem", file="a.yml"),
    ], T)
    ce._assign_to_clusters(clusters, 1, [
        _f("High", 80, "same finding text about a problem", file="b.yml"),
    ], T)
    assert len(clusters) == 2


def test_jaccard_separates_realistic_paraphrase_from_unrelated() -> None:
    """The default threshold must sit between real same-issue paraphrases and
    genuinely different findings, with margin on both sides -- this is the
    property the whole score depends on."""
    same = ce._jaccard(
        ce._keywords("the workflow does not pin the third-party action to a commit SHA"),
        ce._keywords("third-party action is referenced by tag not pinned to a full commit SHA"),
    )
    diff = ce._jaccard(
        ce._keywords("missing timeout on the outbound http request call"),
        ce._keywords("sql injection via unsanitized user input in the query builder"),
    )
    assert same >= T, f"same-issue paraphrase scored {same} < threshold {T}"
    assert diff < T, f"unrelated findings scored {diff} >= threshold {T}"


def test_jaccard_empty_sets() -> None:
    """Two contentless findings are 'the same' (1.0); one empty one not (0.0)."""
    assert ce._jaccard(frozenset(), frozenset()) == 1.0
    assert ce._jaccard(frozenset({"a"}), frozenset()) == 0.0


def test_coarse_key_excludes_line_and_confidence() -> None:
    """The coarse key must be (file, severity, category) only -- including line
    or confidence would fragment the same finding across runs, since both jitter
    at temperature 1.0."""
    a = _f("High", 80, "x", line=100)
    b = _f("High", 20, "x", line=250)
    assert ce._coarse_key(a) == ce._coarse_key(b) == ("x.yml", "High", "authz")
