"""Feedback retention policy — E3.S8.

Rolling window: keep the most recent ``max_count`` entries that are no older
than ``max_age_days``.  Applied atomically during every append so the JSONL
file never grows unbounded.
"""

from __future__ import annotations

import datetime

from ai_pr_review.feedback.models import FeedbackEntry


def apply_retention(
    entries: list[FeedbackEntry],
    *,
    max_count: int = 500,
    max_age_days: int = 365,
) -> list[FeedbackEntry]:
    """Return *entries* filtered and capped by the retention policy.

    *entries* is expected newest-first (as returned by GitBranchStore._parse_jsonl).
    The returned list is also newest-first.
    """
    cutoff = _cutoff_ts(max_age_days)
    kept: list[FeedbackEntry] = []
    for entry in entries:
        if cutoff and entry.ts and entry.ts < cutoff:
            continue
        kept.append(entry)
        if len(kept) >= max_count:
            break
    return kept


def _cutoff_ts(max_age_days: int) -> str | None:
    if max_age_days <= 0:
        return None
    cutoff_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=max_age_days
    )
    return cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
