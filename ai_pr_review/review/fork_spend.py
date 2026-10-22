"""Author cap and global daily spend ledger for fork runs (CAP-2, story 17-7).

Two caps apply to a fork run by a non-trusted author (a ``ForkProfile`` run), on top of the per-run
ceiling and the per-PR ledger of ``fork_budget.py``.

* Author cap: only the 2 lowest-numbered open fork pull requests of one author get an automatic
  review. The state is the list of open pull requests, so nothing is stored.
* Daily cap: 50 USD (``AI_FORK_DAILY_MAX_COST_USD``) per rolling 24 hours. Each run appends one bot
  comment to a dedicated issue. CONTRIBUTOR authors have their own pool
  (``AI_FORK_DAILY_RESERVED_SHARE``, 70 percent). Every other non-trusted author shares the rest.

The daily ledger uses the pattern of story 17-6: append the reserve, re-read the sum, and delete the
own entry and pause when the run does not fit. Admission is ordered by comment ID inside the tier pool.
A run that does not fit appends nothing. A run that never settles (cancelled) stays charged at its
estimate. Trusted authors and non-fork runs never reach this module.

Every failure to read or write pauses the run (``LedgerError``). The GitHub calls are in
``GitHubProvider``. This module is pure logic over the ``SpendBackend`` protocol.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ai_pr_review.review.fork_budget import ForkBudgetPause, LedgerError, format_usd

logger = logging.getLogger(__name__)

SPEND_ISSUE_TITLE = "ai-pr-review fork spend (do not close)"
SPEND_TAG = "ai-pr-review-fork-spend"
TIER_RESERVED, TIER_SHARED = "reserved", "shared"
STATE_RESERVED, STATE_SETTLED = "reserved", "settled"
RESERVED_ASSOCIATION = "CONTRIBUTOR"
CAP_AUTHOR, CAP_DAILY = "author", "daily"
AUTHOR_MAX_PRS = 2
DEFAULT_DAILY_USD = 50.0
DEFAULT_RESERVED_SHARE = 0.7
WINDOW = timedelta(hours=24)
SINCE_MARGIN = timedelta(minutes=5)  # runner clock skew, only widens the server-side listing
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_RUN_RE = re.compile(r"[0-9]{1,20}")

_ENTRY_RE = re.compile(
    rf"<!--\s*{SPEND_TAG}\s+tier=(reserved|shared)\s+run=([0-9]{{1,20}}|none)\s+pr=(\d{{1,10}})\s+"
    r"usd=(\d{1,6})\.(\d{2})\s+state=(reserved|settled)\s+ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*-->")


class SpendBackend(Protocol):
    """The GitHub calls. Every method raises on any failure, never returns a partial result."""

    def list_spend_entries(self, since: str) -> list[tuple[int, str]]:
        """Bot-authored comments with the spend tag on the spend issue, updated at or after *since*
        (an ISO 8601 UTC string). An absent spend issue gives an empty list and creates nothing."""
        ...

    def create_spend_entry(self, body: str) -> int:
        """Find or create the spend issue, then add the comment. Returns the comment ID."""
        ...

    def update_spend_entry(self, comment_id: int, body: str) -> None: ...

    def delete_spend_entry(self, comment_id: int) -> None: ...

    def list_open_pulls(self) -> list[tuple[int, str, str]]:
        """Every open pull request as ``(number, author login, head repository full name)``.
        The head repository name is empty for a deleted fork."""
        ...


@dataclass(frozen=True)
class DailyCaps:
    max_cents: int = round(DEFAULT_DAILY_USD * 100)
    reserved_share: float = DEFAULT_RESERVED_SHARE

    @classmethod
    def from_usd(cls, max_usd: float, reserved_share: float) -> DailyCaps:
        return cls(max_cents=max(0, round(max_usd * 100)), reserved_share=reserved_share)

    def pool_cents(self, tier: str) -> int:
        """A strict partition. The reserved pool rounds down, the shared pool takes the rest."""
        reserved = int(self.max_cents * self.reserved_share)
        return reserved if tier == TIER_RESERVED else self.max_cents - reserved


def tier_of(association: str) -> str:
    """CONTRIBUTOR has the reserved pool. Every other association, and an empty one, shares the rest."""
    return TIER_RESERVED if association.strip().upper() == RESERVED_ASSOCIATION else TIER_SHARED


@dataclass(frozen=True)
class SpendEntry:
    comment_id: int
    tier: str
    pr: int
    cents: int
    state: str
    ts: datetime


@dataclass(frozen=True)
class SpendReservation:
    comment_id: int
    estimate_cents: int
    ts: datetime


def _format_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_spend_entry(tier: str, run_id: str, pr: int, cents: int, state: str, ts: datetime) -> str:
    run = run_id if _RUN_RE.fullmatch(run_id) else "none"
    marker = (f"<!-- {SPEND_TAG} tier={tier} run={run} pr={pr} usd={format_usd(cents)} "
              f"state={state} ts={_format_ts(ts)} -->")
    return (f"{marker}\nFork review spend entry for pull request #{pr}. "
            "The bot keeps this comment. Do not edit or delete it.")


def parse_spend_entry(comment_id: int, body: str) -> SpendEntry:
    """Parse a bot-authored spend comment. A body that holds the tag but not a valid marker is a
    ``LedgerError``, because skipping it would undercount."""
    match = _ENTRY_RE.search(body)
    if match is None:
        raise LedgerError(f"comment {comment_id} holds the spend tag but no valid marker")
    try:
        ts = datetime.strptime(match.group(7), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise LedgerError(f"comment {comment_id} holds an invalid timestamp") from exc
    return SpendEntry(
        comment_id=comment_id, tier=match.group(1), pr=int(match.group(3)),
        cents=int(match.group(4)) * 100 + int(match.group(5)), state=match.group(6), ts=ts)


def window_start(now: datetime) -> datetime:
    return now - WINDOW


def in_window(entries: list[SpendEntry], now: datetime) -> list[SpendEntry]:
    cutoff = window_start(now)
    return [e for e in entries if e.ts >= cutoff]


def _tier_sum(entries: list[SpendEntry], tier: str, *, through_id: int | None = None) -> int:
    return sum(e.cents for e in entries
               if e.tier == tier and (through_id is None or e.comment_id <= through_id))


def daily_pause_text(tier: str, used_cents: int, caps: DailyCaps) -> str:
    pool = caps.pool_cents(tier)
    group = ("authors with the CONTRIBUTOR association" if tier == TIER_RESERVED
             else "all other non-trusted authors")
    return (
        "The review of this fork pull request is paused. The daily review budget for fork pull requests "
        f"is used up. The cap is {format_usd(caps.max_cents)} USD in 24 hours (`AI_FORK_DAILY_MAX_COST_USD`). "
        f"The share for {group} is {format_usd(pool)} USD, and {format_usd(used_cents)} USD is used. "
        "The pause clears as the 24 hour window moves on. Run the workflow again later. "
        "A rescan does not change the daily cap.")


class ForkSpend:
    """Daily spend ledger for one fork run. Build it with a backend, the caps, the author tier,
    the pull request number and the run ID. ``clock`` is a seam for tests."""

    def __init__(
        self, backend: SpendBackend, caps: DailyCaps, *, tier: str, pr_number: int, run_id: str = "",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend
        self._caps = caps
        self._tier = tier
        self._pr = pr_number
        self._run = run_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def _scan(self) -> tuple[list[SpendEntry], datetime]:
        now = self._clock()
        since = _format_ts(window_start(now) - SINCE_MARGIN)
        try:
            raw = self._backend.list_spend_entries(since)
        except Exception as exc:  # noqa: BLE001 - any read failure must pause the run
            raise LedgerError(f"could not read the spend ledger ({type(exc).__name__})") from exc
        entries = sorted((parse_spend_entry(cid, body) for cid, body in raw), key=lambda e: e.comment_id)
        return in_window(entries, now), now

    def precheck(self) -> None:
        """Pause when the own tier pool has no cent left. Read only, runs before any analyzer."""
        entries, _ = self._scan()
        used = _tier_sum(entries, self._tier)
        if used >= self._caps.pool_cents(self._tier):
            raise ForkBudgetPause(CAP_DAILY, daily_pause_text(self._tier, used, self._caps))

    def reserve(self, estimate_cents: int) -> SpendReservation:
        """Append the estimate, re-read, and keep the entry only when it is admitted.

        A run that does not fit appends nothing. A run that loses the ordering deletes its own entry.
        If that delete fails the entry stays charged, which is the safe direction.
        """
        pool = self._caps.pool_cents(self._tier)
        entries, now = self._scan()
        used = _tier_sum(entries, self._tier)
        if used >= pool or used + estimate_cents > pool:  # no cent left, or the estimate does not fit
            raise ForkBudgetPause(CAP_DAILY, daily_pause_text(self._tier, used, self._caps))
        body = render_spend_entry(self._tier, self._run, self._pr, estimate_cents, STATE_RESERVED, now)
        try:
            own_id = self._backend.create_spend_entry(body)
        except Exception as exc:  # noqa: BLE001 - any write failure must pause the run
            raise LedgerError(f"could not write the spend ledger ({type(exc).__name__})") from exc
        entries, _ = self._scan()
        if not any(e.comment_id == own_id for e in entries):
            raise LedgerError("the own spend entry is gone, so admission cannot be checked")
        through = _tier_sum(entries, self._tier, through_id=own_id)
        if through > pool:
            try:
                self._backend.delete_spend_entry(own_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("fork spend: could not delete the losing entry (%s). It stays charged.",
                               type(exc).__name__)
            raise ForkBudgetPause(CAP_DAILY, daily_pause_text(self._tier, through - estimate_cents, self._caps))
        return SpendReservation(own_id, estimate_cents, now)

    def settle(self, reservation: SpendReservation, actual_cents: int) -> None:
        """Replace the estimate in the own entry with the actual cost. Raises ``LedgerError`` on failure.
        The caller keeps the estimate charged in that case."""
        body = render_spend_entry(
            self._tier, self._run, self._pr, actual_cents, STATE_SETTLED, reservation.ts)
        try:
            self._backend.update_spend_entry(reservation.comment_id, body)
        except Exception as exc:  # noqa: BLE001
            raise LedgerError(f"could not settle the spend entry ({type(exc).__name__})") from exc

    def release(self, reservation: SpendReservation) -> None:
        """Delete the own entry of a run that will not start. Best effort: a failed delete stays charged."""
        try:
            self._backend.delete_spend_entry(reservation.comment_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fork spend: could not release the entry (%s). It stays charged.", type(exc).__name__)


def author_pause_text(author: str, open_count: int) -> str:
    who = f"The author `{author}`" if _LOGIN_RE.fullmatch(author) else "The author"
    return (
        "The review of this fork pull request is paused. The engine reviews only the "
        f"{AUTHOR_MAX_PRS} open fork pull requests with the lowest numbers for one author. "
        f"{who} has {open_count} open fork pull requests, and this one is not among the first "
        f"{AUTHOR_MAX_PRS}. The review starts when an earlier pull request of this author is closed or merged.")


def check_author_cap(backend: SpendBackend, *, pr_number: int, author: str, base_repo: str) -> None:
    """Pause when this pull request is not among the lowest-numbered open fork pull requests of its author.

    Raises ``ForkBudgetPause`` at the cap and ``LedgerError`` when the list cannot be read, the author is
    unknown or this pull request is not in the open list. A fork is a head repository that differs from
    the base repository, and a deleted head repository counts as a fork.
    """
    if not author:
        raise LedgerError("the author login is unknown, so the author cap cannot be checked")
    try:
        pulls = backend.list_open_pulls()
    except Exception as exc:  # noqa: BLE001 - any read failure must pause the run
        raise LedgerError(f"could not read the open pull requests ({type(exc).__name__})") from exc
    mine = sorted(
        number for number, login, head_repo in pulls
        if login.casefold() == author.casefold() and (not head_repo or head_repo.casefold() != base_repo.casefold()))
    if pr_number not in mine:
        raise LedgerError("this pull request is not in the open fork pull requests of its author")
    if mine.index(pr_number) >= AUTHOR_MAX_PRS:
        raise ForkBudgetPause(CAP_AUTHOR, author_pause_text(author, len(mine)))
