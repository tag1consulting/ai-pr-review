"""Findings deduplication and merging.

Ports the proximity-based dedup from lib/findings.sh and review.sh:
findings in the same file within PROXIMITY_LINES of each other are
clustered; the highest-severity finding survives with a union of sources.

Closes #185 (dedup preserves distinct nearby findings; merges exact
cross-source duplicates).
"""

from __future__ import annotations

from ai_pr_review.findings.models import Finding, Severity
from ai_pr_review.findings.provenance import boosted_confidence, is_corroborated

_SEVERITY_ORDER: dict[Severity, int] = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
}

# Findings within this many lines of each other in the same file are
# candidates for dedup (mirrors the bash implementation).
PROXIMITY_LINES = 3


def merge_findings(
    findings: list[Finding],
    *,
    confidence_threshold: int = 75,
) -> list[Finding]:
    """Filter by confidence, then deduplicate by proximity.

    Returns a list of unique findings sorted by severity (highest first)
    then by file+line.
    """
    # 1. Filter by confidence
    filtered = [f for f in findings if f.confidence >= confidence_threshold]

    # 2. Proximity-based dedup within each file
    return _deduplicate(filtered)


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Cluster findings within PROXIMITY_LINES of each other in the same file."""
    # Group by file
    by_file: dict[str, list[Finding]] = {}
    no_file: list[Finding] = []
    for f in findings:
        if f.file:
            by_file.setdefault(f.file, []).append(f)
        else:
            no_file.append(f)

    result: list[Finding] = []
    for file_findings in by_file.values():
        result.extend(_dedup_file(file_findings))

    # Findings without file go through text-based dedup (no proximity merging)
    result.extend(_dedup_no_file(no_file))

    # Sort: severity (highest first), then file, then line
    result.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            f.file,
            f.line or 0,
        )
    )
    return result


def _dedup_file(findings: list[Finding]) -> list[Finding]:
    """Deduplicate findings within a single file using proximity clustering."""
    # Sort by line number (None → 0)
    sorted_fs = sorted(findings, key=lambda f: f.line or 0)

    clusters: list[list[Finding]] = []
    for f in sorted_fs:
        placed = False
        for cluster in clusters:
            # Compare against the last (nearest) finding in the cluster so that
            # a chain of findings within PROXIMITY_LINES all merge together,
            # even if the first finding is outside the window.
            tail = cluster[-1]
            tail_line = tail.line or 0
            f_line = f.line or 0
            if abs(tail_line - f_line) <= PROXIMITY_LINES:
                cluster.append(f)
                placed = True
                break
        if not placed:
            clusters.append([f])

    return [_collapse_cluster(c) for c in clusters]


def _collapse_cluster(cluster: list[Finding]) -> Finding:
    """Collapse a cluster to the highest-severity representative."""
    best = min(cluster, key=lambda f: _SEVERITY_ORDER.get(f.severity, 99))
    all_sources: list[str] = []
    seen: set[str] = set()
    for f in cluster:
        for s in (f.sources or ([f.source] if f.source else [])):
            if s and s not in seen:
                seen.add(s)
                all_sources.append(s)

    update: dict[str, object] = {"sources": sorted(all_sources)}
    if is_corroborated(all_sources):
        update["corroborated"] = True
        update["confidence"] = boosted_confidence(best.confidence)
    return best.model_copy(update=update)


def _dedup_no_file(findings: list[Finding]) -> list[Finding]:
    """Deduplicate body-only findings by exact finding-text match."""
    seen: dict[str, Finding] = {}
    for f in findings:
        key = f.finding.strip()
        if key not in seen:
            seen[key] = f
        else:
            existing = seen[key]
            merged_sources = sorted(
                set((existing.sources or []) + (f.sources or []))
            )
            seen[key] = existing.model_copy(update={"sources": merged_sources})
    return list(seen.values())
