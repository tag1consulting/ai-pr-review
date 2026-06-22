"""Provenance weighting — confidence boost for corroborated findings.

A finding is *corroborated* when its merged source cluster contains at least
one native static-analyzer source AND at least one LLM-agent source.
Independent agreement between tools from different categories is a strong
signal that the finding is real.

This module is a pure, deterministic helper: no I/O, no LLM calls, zero token
impact.  It is called from ``merge._collapse_cluster`` after source-union has
been built.
"""

from __future__ import annotations

from ai_pr_review.agents.roster import AGENT_NAMES
from ai_pr_review.findings.scope import is_analyzer_source

#: Additive confidence delta applied to corroborated findings (capped at 100).
PROVENANCE_BOOST: int = 10


def is_corroborated(sources: list[str]) -> bool:
    """Return True when *sources* contains >=1 analyzer tag AND >=1 agent name.

    Uses :func:`~ai_pr_review.findings.scope.is_analyzer_source` for the
    analyzer side (keeping ``_ANALYZER_PREFIXES`` as the single source of
    truth) and ``AGENT_NAMES`` for the LLM-agent side.

    Args:
        sources: The unioned source list from a merged finding cluster.

    Returns:
        ``True`` iff the cluster has cross-category corroboration.
    """
    has_analyzer = any(is_analyzer_source(s) for s in sources)
    has_agent = any(s in AGENT_NAMES for s in sources)
    return has_analyzer and has_agent


def boosted_confidence(confidence: int) -> int:
    """Return *confidence* incremented by :data:`PROVENANCE_BOOST`, capped at 100.

    Args:
        confidence: Original confidence value (0–100).

    Returns:
        Boosted value, never exceeding 100.
    """
    return min(100, confidence + PROVENANCE_BOOST)
