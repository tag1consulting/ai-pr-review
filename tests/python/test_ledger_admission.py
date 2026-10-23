"""Pure tests for the shared ledger admission steps."""

from __future__ import annotations

import logging

import pytest

from ai_pr_review.review._ledger_admission import (
    LedgerError,
    append_and_reread,
    entries_through,
    release_quietly,
    require_own_entry,
)


def test_entries_through_keeps_ids_up_to_and_including_the_own_one() -> None:
    assert entries_through([1, 5, 7, 9], 7, lambda e: e) == [1, 5, 7]


def test_require_own_entry_raises_when_the_own_id_is_missing() -> None:
    require_own_entry([1, 2], 2, "ledger")
    with pytest.raises(LedgerError, match="own ledger entry is gone"):
        require_own_entry([1, 3], 2, "ledger")


def test_release_quietly_logs_only_the_exception_type(caplog: pytest.LogCaptureFixture) -> None:
    def boom(_: int) -> None:
        raise RuntimeError("secret body text")

    with caplog.at_level(logging.WARNING):
        release_quietly(boom, 4, label="fork test", why="of a test")
    assert "RuntimeError" in caplog.text and "stays charged" in caplog.text
    assert "secret body text" not in caplog.text


def test_append_and_reread_returns_the_own_id_and_the_entries() -> None:
    deleted: list[int] = []
    own, entries = append_and_reread(lambda: 5, lambda: [3, 5], deleted.append, lambda e: e, label="x")
    assert (own, entries, deleted) == (5, [3, 5], [])


def test_append_and_reread_releases_the_own_entry_on_a_failed_reread() -> None:
    deleted: list[int] = []

    def reread() -> list[int]:
        raise LedgerError("read failed")

    with pytest.raises(LedgerError, match="read failed"):
        append_and_reread(lambda: 5, reread, deleted.append, lambda e: e, label="x")
    assert deleted == [5]


def test_append_and_reread_does_not_release_when_the_own_entry_is_missing() -> None:
    deleted: list[int] = []
    with pytest.raises(LedgerError, match="gone"):
        append_and_reread(lambda: 5, lambda: [3], deleted.append, lambda e: e, label="x")
    assert deleted == []
