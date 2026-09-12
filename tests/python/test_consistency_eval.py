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


# ---------------------------------------------------------------------------
# Corpus-mode additions (issue #800): diff-to-changed-files parsing and the
# free, deterministic gate-firing readout. Both run with zero network access,
# unlike the model-dispatch path these feed into.
# ---------------------------------------------------------------------------


def test_changed_files_from_diff_extracts_post_image_paths() -> None:
    diff_text = (
        "diff --git a/src/a.py b/src/a.py\n"
        "index abc..def 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+new line\n"
        "diff --git a/docs/b.md b/docs/b.md\n"
        "--- a/docs/b.md\n"
        "+++ b/docs/b.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert ce._changed_files_from_diff(diff_text) == ["src/a.py", "docs/b.md"]


def test_changed_files_from_diff_skips_deleted_files() -> None:
    """A deleted file's post-image is /dev/null -- there is no content left to
    review, so it must not appear in the changed-files list gate evaluation
    and manifest categorization act on."""
    diff_text = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-gone\n"
    )
    assert ce._changed_files_from_diff(diff_text) == []


def test_changed_files_from_diff_new_file_has_no_a_prefix_to_strip() -> None:
    """A new file's pre-image is /dev/null; the post-image path still uses the
    'b/' prefix this parser strips."""
    diff_text = (
        "diff --git a/new.go b/new.go\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/new.go\n"
        "@@ -0,0 +1,1 @@\n"
        "+package main\n"
    )
    assert ce._changed_files_from_diff(diff_text) == ["new.go"]


def test_gate_report_for_diff_docs_only_fires_no_content_gates(tmp_path: Path) -> None:
    """A pure-markdown diff should not trip has_code_or_infra, has_control_flow,
    has_error_patterns, or has_security_patterns -- confirms the corpus's
    docs-only fixture actually exercises the cheap-diff path it's there for."""
    diff_file = tmp_path / "docs_only.diff"
    diff_file.write_text(
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,1 +1,2 @@\n"
        " # Title\n"
        "+A new line of prose.\n"
    )
    fired, would_run = ce._gate_report_for_diff(diff_file)
    assert "has_code_or_infra" not in fired
    assert "has_error_patterns" not in fired
    assert "has_security_patterns" not in fired
    assert "code-reviewer" in would_run  # always-eligible agent, no conditional_trigger


def test_gate_report_for_diff_security_path_fires_security_gate(tmp_path: Path) -> None:
    """A diff touching a dependency manifest should fire has_security_patterns,
    confirming the corpus's real security-relevant fixtures actually promote
    security-reviewer the way production would."""
    diff_file = tmp_path / "security.diff"
    diff_file.write_text(
        "diff --git a/package.json b/package.json\n"
        "--- a/package.json\n"
        "+++ b/package.json\n"
        "@@ -1,3 +1,3 @@\n"
        " {\n"
        '-  \"lodash\": \"4.17.20\"\n'
        '+  \"lodash\": \"4.17.21\"\n'
        " }\n"
    )
    fired, _ = ce._gate_report_for_diff(diff_file)
    assert "has_security_patterns" in fired


def test_as_finding_like_adapter_bridges_severity_only() -> None:
    """The Protocol adapter classify_review_outcome needs must expose exactly
    the severity string, unchanged, regardless of the finding's other fields."""
    f = _f("Critical", 90, "something bad")
    adapted = ce._AsFindingLike(f)
    assert adapted.severity == "Critical"
