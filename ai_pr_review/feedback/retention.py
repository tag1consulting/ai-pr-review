"""Feedback retention policy — E3.S8.

Rolling window: keep the most recent ``max_count`` entries that are no older
than ``max_age_days``.  Applied atomically during every append so the JSONL
file never grows unbounded.
"""

from __future__ import annotations

import datetime
import logging

from ai_pr_review.feedback.models import FeedbackEntry

logger = logging.getLogger(__name__)


def apply_retention(
    entries: list[FeedbackEntry],
    *,
    max_count: int = 500,
    max_age_days: int = 365,
) -> list[FeedbackEntry]:
    """Return *entries* filtered and capped by the retention policy.

    *entries* is expected newest-first (as returned by GitBranchStore._parse_jsonl).
    The returned list is also newest-first.

    Timestamp handling: ``entry.ts`` is parsed via ``datetime.fromisoformat`` so
    both ``...Z`` and ``...+00:00`` UTC formats compare correctly.  Entries with
    an unparseable timestamp are kept (no false-positive drops on legacy data).
    """
    cutoff_dt = _cutoff_dt(max_age_days)
    kept: list[FeedbackEntry] = []
    for entry in entries:
        if cutoff_dt is not None and entry.ts:
            entry_dt = _parse_ts(entry.ts)
            if entry_dt is not None and entry_dt < cutoff_dt:
                continue
        kept.append(entry)
        if len(kept) >= max_count:
            break
    return kept


def _cutoff_dt(max_age_days: int) -> datetime.datetime | None:
    if max_age_days <= 0:
        return None
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=max_age_days)


def _parse_ts(ts: str) -> datetime.datetime | None:
    """Parse an ISO-8601 UTC timestamp.  Tolerates both 'Z' and '+00:00' forms.

    Returns ``None`` on parse failure (caller treats this as "keep the entry"
    rather than drop it — better to keep stale data than silently delete it).
    """
    try:
        # fromisoformat accepts '+00:00' natively; rewrite trailing 'Z' for it.
        normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        logger.debug("retention: could not parse timestamp %r; entry kept", ts)
        return None
    if dt.tzinfo is None:
        # Naive datetime — assume UTC rather than raise (lenient).
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt
