"""Shared append, re-read, admit-by-ID, release-own steps for the fork ledgers.

Both ``fork_budget.py`` (per-PR ledger) and ``fork_spend.py`` (daily spend ledger) admit a run the
same way. The run appends its own entry, reads the whole ledger again, and keeps the entry only when
the running sum up to its own comment ID fits the caps. A run that loses deletes its own entry.

This module holds the steps the two ledgers share. The cap arithmetic stays in each ledger.

* ``append_and_reread``: append, re-read, and check that the own entry is in the re-read list.
  A failed re-read releases the own entry (best effort) and re-raises. A stale list that omits the
  own entry is a ``LedgerError`` and releases nothing, because the entry may still be charged.
* ``entries_through``: the entries up to and including the own comment ID.
* ``release_quietly``: best-effort delete of the own entry. A failed delete leaves the entry charged,
  which is the safe direction. It logs the exception type only.

Intentional difference that stays outside this module: how each ledger treats a malformed bot
comment. The per-PR ledger ignores one, because the bot's own summaries can quote fork text and so
contain the ledger tag. The spend issue holds only entries, so it fails closed with a ``LedgerError``
on a malformed entry, because skipping it would undercount the daily spend.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LedgerError(Exception):
    """The ledger could not be read, written or understood. A fork run pauses."""


def release_quietly(delete: Callable[[int], None], own_id: int, *, label: str, why: str) -> None:
    """Delete the own entry on a best-effort basis. A failed delete stays charged."""
    try:
        delete(own_id)
    except Exception as exc:  # noqa: BLE001 - the release is best effort
        logger.warning("%s: could not delete the own entry %s (%s). It stays charged.",
                       label, why, type(exc).__name__)


def require_own_entry(comment_ids: list[int], own_id: int, label: str) -> None:
    """Raise ``LedgerError`` when the own entry is not in the list, because admission cannot be checked."""
    if own_id not in comment_ids:
        raise LedgerError(f"the own {label} entry is gone, so admission cannot be checked")


def entries_through(entries: list[T], own_id: int, id_of: Callable[[T], int]) -> list[T]:
    """The entries admission counts: every entry with a comment ID up to the own one."""
    return [e for e in entries if id_of(e) <= own_id]


def append_and_reread(
    create: Callable[[], int],
    reread: Callable[[], list[T]],
    delete: Callable[[int], None],
    id_of: Callable[[T], int],
    *,
    label: str,
) -> tuple[int, list[T]]:
    """Append the own entry, read the ledger again, and return ``(own_id, entries)``.

    ``create`` and ``reread`` raise ``LedgerError`` on failure. A failed re-read releases the own
    entry and re-raises. Admission against the caps is the caller's step.
    """
    own_id = create()
    try:
        entries = reread()
    except LedgerError:
        release_quietly(delete, own_id, label=label, why="after a failed re-read")
        raise
    require_own_entry([id_of(e) for e in entries], own_id, label)
    return own_id, entries
