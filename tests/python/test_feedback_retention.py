"""Tests for ai_pr_review.feedback.retention — E3.S8."""

import datetime

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.feedback.retention import apply_retention


def _entry(ts: str, command: str = "feedback") -> FeedbackEntry:
    return FeedbackEntry(ts=ts, command=command, reason="r", source="s")


def test_empty_input() -> None:
    assert apply_retention([], max_count=10, max_age_days=30) == []


def test_under_count_limit_all_kept() -> None:
    entries = [_entry(f"2026-05-{i:02d}T00:00:00Z") for i in range(1, 6)]
    kept = apply_retention(entries, max_count=10, max_age_days=0)
    assert len(kept) == 5


def test_count_limit_applied() -> None:
    entries = [_entry(f"2026-05-{i:02d}T00:00:00Z") for i in range(1, 11)]
    kept = apply_retention(entries, max_count=3, max_age_days=0)
    assert len(kept) == 3


def test_age_limit_drops_old_entries() -> None:
    today = datetime.datetime.now(datetime.timezone.utc)
    old_ts = (today - datetime.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_ts = today.strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [_entry(old_ts), _entry(new_ts)]
    kept = apply_retention(entries, max_count=100, max_age_days=365)
    # Only the new entry should survive
    assert len(kept) == 1
    assert kept[0].ts == new_ts


def test_age_limit_zero_keeps_all() -> None:
    today = datetime.datetime.now(datetime.timezone.utc)
    old_ts = (today - datetime.timedelta(days=1000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [_entry(old_ts)]
    kept = apply_retention(entries, max_count=100, max_age_days=0)
    assert len(kept) == 1


def test_count_applied_before_adding_more() -> None:
    # entries is newest-first (as returned by store)
    entries = [
        _entry("2026-05-14T00:00:00Z"),
        _entry("2026-05-13T00:00:00Z"),
        _entry("2026-05-12T00:00:00Z"),
    ]
    kept = apply_retention(entries, max_count=2, max_age_days=0)
    assert len(kept) == 2
    # Should keep newest two
    assert kept[0].ts == "2026-05-14T00:00:00Z"
    assert kept[1].ts == "2026-05-13T00:00:00Z"
