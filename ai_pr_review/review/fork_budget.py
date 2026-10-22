"""Per-pull-request spend ledger for fork runs (CAP-2, story 17-6).

A fork run by a non-trusted author is bounded per run (``AI_FORK_MAX_COST_USD``) and, here, per
pull request: 4 runs and 6 USD. The ledger lives in hidden comments that only the bot wrote. The
settled marker is ``<!-- ai-pr-review-fork-ledger runs=N usd=X.XX allowance=A -->``.

One comment per reservation, not one shared comment. A shared comment that is updated in place
cannot stop two concurrent runs: both read N, both write N+1, both re-read their own value and
pass. Here each run appends its own entry. The ledger is the sum of every bot-authored entry.
Admission is ordered by comment ID: an entry passes only when the running sum up to its own ID
fits the caps. A run that does not fit deletes its own entry and pauses. Story 17-7 reuses this
append, re-read, remove-own pattern for the daily cap.

* ``precheck``: before any analyzer. Credits maintainer rescan comments, then pauses at a cap.
* ``reserve``: after the cost estimate. Appends an entry for the estimate, re-reads, verifies.
* ``settle``: after the run. Replaces the estimate in the own entry with the actual cost.
  A run that never settles (cancelled) stays charged at its estimate.

The rescan allowance is derived, not stored by the workflow. Every comment from an OWNER, MEMBER or
COLLABORATOR that routes as ``/ai-pr-review rescan`` gets one credit entry keyed by its comment
ID, so a re-run of a job or a race credits a comment once. One credit adds one run and one run
ceiling (``Caps.run_cents``) to the caps. The ledger is never reset.

Every failure to read or write pauses the run (``LedgerError``). The comment calls are in
``GitHubProvider``. This module is pure logic over the ``BudgetBackend`` protocol.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

LEDGER_TAG = "ai-pr-review-fork-ledger"
RESCAN_TAG = "ai-pr-review-fork-rescan"
PR_MAX_RUNS = 4
PR_MAX_CENTS = 600
DEFAULT_RUN_CENTS = 200
# The same set the slash-commands workflow lets run a rescan (handle-command job).
MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
CAP_RUNS, CAP_USD = "runs", "usd"

_ENTRY_RE = re.compile(
    rf"<!--\s*{LEDGER_TAG}\s+runs=(\d{{1,6}})\s+usd=(\d{{1,6}})\.(\d{{2}})\s+allowance=(\d{{1,6}})\s*-->")
_RESCAN_RE = re.compile(rf"<!--\s*{RESCAN_TAG}\s+comment=(\d{{1,20}})\s*-->")

LEDGER_ERROR_TEXT = (
    "The review of this fork pull request is paused. The review budget for this pull request could not "
    "be read or written, so the run does not start. Run the workflow again. The gate stays closed when "
    "it cannot check the budget.")
ESTIMATE_ERROR_TEXT = (
    "The review of this fork pull request is paused. The cost of the review could not be estimated, "
    "so the run does not start. Run the workflow again.")


class LedgerError(Exception):
    """The ledger could not be read, written or understood. A fork run pauses."""


class ForkBudgetPause(Exception):
    """A cap stops the run. ``cap`` is ``runs`` or ``usd``. ``reason`` is the text for the skip comment."""

    def __init__(self, cap: str, reason: str) -> None:
        super().__init__(reason)
        self.cap = cap
        self.reason = reason


class BudgetBackend(Protocol):
    """The comment calls. Every method raises on any failure, never returns a partial result."""

    def list_budget_comments(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """Return ``(ledger, maintainer)``. ``ledger`` is every bot-authored comment whose body holds
        the ledger tag. ``maintainer`` is every non-bot comment from a maintainer association."""
        ...

    def create_budget_entry(self, body: str) -> int: ...

    def update_budget_entry(self, comment_id: int, body: str) -> None: ...

    def delete_budget_entry(self, comment_id: int) -> None: ...


@dataclass(frozen=True)
class Caps:
    runs: int = PR_MAX_RUNS
    cents: int = PR_MAX_CENTS
    run_cents: int = DEFAULT_RUN_CENTS  # the per-run ceiling, added to the cents cap per allowance unit

    def effective(self, allowance: int) -> tuple[int, int]:
        return self.runs + allowance, self.cents + allowance * self.run_cents


@dataclass(frozen=True)
class Entry:
    comment_id: int
    runs: int
    cents: int
    allowance: int
    rescan_id: int | None = None  # set on a credit entry, the maintainer comment it credits


@dataclass(frozen=True)
class Reservation:
    comment_id: int
    estimate_cents: int


def cents_from_units(units: int) -> int:
    """Convert $0.0001 units (``pricing``) to whole cents, rounding up so the ledger never undercharges."""
    return max(0, -(-units // 100))


def format_usd(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def render_entry(runs: int, cents: int, allowance: int, rescan_id: int | None = None) -> str:
    marker = f"<!-- {LEDGER_TAG} runs={runs} usd={format_usd(cents)} allowance={allowance} -->"
    if rescan_id is not None:
        marker += f"\n<!-- {RESCAN_TAG} comment={rescan_id} -->"
    return f"{marker}\nFork review budget entry. The bot keeps this comment. Do not edit it."


def parse_entry(comment_id: int, body: str) -> Entry:
    """Parse a bot-authored ledger comment. A body that holds the tag but not a valid marker is a
    ``LedgerError``, because skipping it would undercount."""
    match = _ENTRY_RE.search(body)
    if match is None:
        raise LedgerError(f"comment {comment_id} holds the ledger tag but no valid marker")
    rescan = _RESCAN_RE.search(body)
    return Entry(
        comment_id=comment_id,
        runs=int(match.group(1)),
        cents=int(match.group(2)) * 100 + int(match.group(3)),
        allowance=int(match.group(4)),
        rescan_id=int(rescan.group(1)) if rescan else None,
    )


def allowance_of(entries: list[Entry]) -> int:
    """Credits are counted once per rescan comment ID, whatever the number of entries for it."""
    credited = {e.rescan_id for e in entries if e.rescan_id is not None}
    return len(credited) + sum(e.allowance for e in entries if e.rescan_id is None)


def is_rescan_comment(body: str) -> bool:
    """True when the slash-commands workflow would route the comment as ``rescan``."""
    if not body.lstrip().startswith("/ai-pr-review"):
        return False
    from ai_pr_review.slash.handlers import parse_command_gate_lines

    lines = parse_command_gate_lines(body)
    return "command=rescan" in lines and "valid=true" in lines


def _limits(entries: list[Entry], caps: Caps) -> tuple[int, int]:
    return caps.effective(allowance_of(entries))


def at_cap(entries: list[Entry], caps: Caps) -> str | None:
    """The precheck: name the cap when the ledger has no whole run or cent left."""
    max_runs, max_cents = _limits(entries, caps)
    if sum(e.runs for e in entries) >= max_runs:
        return CAP_RUNS
    if sum(e.cents for e in entries) >= max_cents:
        return CAP_USD
    return None


def cannot_fit(entries: list[Entry], caps: Caps, estimate_cents: int) -> str | None:
    """Name the cap that one more run of ``estimate_cents`` would pass. Nothing is written for it."""
    max_runs, max_cents = _limits(entries, caps)
    if sum(e.runs for e in entries) + 1 > max_runs:
        return CAP_RUNS
    if sum(e.cents for e in entries) + estimate_cents > max_cents:
        return CAP_USD
    return None


def not_admitted(entries: list[Entry], caps: Caps, own_id: int) -> str | None:
    """The admission rule: the running sum up to the own entry, in comment ID order, must fit the caps.
    All allowance counts. A missing own entry is a ``LedgerError``, because admission cannot be checked."""
    counted = [e for e in entries if e.comment_id <= own_id]
    if not any(e.comment_id == own_id for e in counted):
        raise LedgerError("the own ledger entry is gone, so admission cannot be checked")
    max_runs, max_cents = _limits(entries, caps)
    if sum(e.runs for e in counted) > max_runs:
        return CAP_RUNS
    if sum(e.cents for e in counted) > max_cents:
        return CAP_USD
    return None


def pause_text(cap: str, entries: list[Entry], caps: Caps, *, estimate_cents: int = 0) -> str:
    max_runs, max_cents = caps.effective(allowance_of(entries))
    used_runs, used_cents = sum(e.runs for e in entries), sum(e.cents for e in entries)
    resume = "A maintainer with write access can comment `/ai-pr-review rescan` to allow one more review run."
    if cap == CAP_RUNS:
        return (
            "The review of this fork pull request is paused. It reached the cap of "
            f"{max_runs} review runs for one pull request ({used_runs} used). {resume}")
    more = f" The next review is estimated at {format_usd(estimate_cents)} USD." if estimate_cents else ""
    return (
        "The review of this fork pull request is paused. It reached the cap of "
        f"{format_usd(max_cents)} USD of review cost for one pull request ({format_usd(used_cents)} USD used)."
        f"{more} {resume}")


class ForkBudget:
    """Reserve, settle and precheck for one pull request. Build it with a backend and the caps."""

    def __init__(self, backend: BudgetBackend, caps: Caps | None = None) -> None:
        self._backend = backend
        self._caps = caps or Caps()

    def _scan(self) -> tuple[list[Entry], list[tuple[int, str]]]:
        try:
            ledger, maintainer = self._backend.list_budget_comments()
        except Exception as exc:  # noqa: BLE001 - any read failure must pause the run
            raise LedgerError(f"could not read the ledger ({type(exc).__name__})") from exc
        entries = sorted((parse_entry(cid, body) for cid, body in ledger), key=lambda e: e.comment_id)
        return entries, maintainer

    def _create(self, body: str) -> int:
        try:
            return self._backend.create_budget_entry(body)
        except Exception as exc:  # noqa: BLE001 - any write failure must pause the run
            raise LedgerError(f"could not write the ledger ({type(exc).__name__})") from exc

    def precheck(self) -> list[Entry]:
        """Credit maintainer rescan comments, then pause when the pull request is at a cap.

        Runs before any analyzer or git command touches the fork tree. Raises ``ForkBudgetPause`` or
        ``LedgerError``. Returns the entries it read.
        """
        entries, maintainer = self._scan()
        credited = {e.rescan_id for e in entries if e.rescan_id is not None}
        for comment_id, body in maintainer:
            if comment_id in credited or not is_rescan_comment(body):
                continue
            new_id = self._create(render_entry(0, 0, 1, comment_id))
            entries.append(Entry(new_id, 0, 0, 1, comment_id))
            credited.add(comment_id)
            logger.info("fork budget: credited rescan comment %d with one more run", comment_id)
        cap = at_cap(entries, self._caps)
        if cap is not None:
            raise ForkBudgetPause(cap, pause_text(cap, entries, self._caps))
        return entries

    def reserve(self, estimate_cents: int) -> Reservation:
        """Append an entry for the estimate, re-read, and keep it only when it is admitted.

        A run that does not fit appends nothing. A run that loses the ordering deletes its own entry.
        If that delete fails, the entry stays charged, which is the safe direction.
        """
        entries, _ = self._scan()
        cap = cannot_fit(entries, self._caps, estimate_cents)
        if cap is not None:
            raise ForkBudgetPause(cap, pause_text(cap, entries, self._caps, estimate_cents=estimate_cents))
        own_id = self._create(render_entry(1, estimate_cents, 0))
        entries, _ = self._scan()
        cap = not_admitted(entries, self._caps, own_id)
        if cap is not None:
            try:
                self._backend.delete_budget_entry(own_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("fork budget: could not delete the losing entry (%s). It stays charged.",
                               type(exc).__name__)
            raise ForkBudgetPause(cap, pause_text(
                cap, [e for e in entries if e.comment_id <= own_id], self._caps, estimate_cents=estimate_cents))
        return Reservation(own_id, estimate_cents)

    def settle(self, reservation: Reservation, actual_cents: int) -> None:
        """Replace the estimate in the own entry with the actual cost. Raises ``LedgerError`` on failure.
        The caller keeps the estimate charged in that case."""
        try:
            self._backend.update_budget_entry(reservation.comment_id, render_entry(1, actual_cents, 0))
        except Exception as exc:  # noqa: BLE001
            raise LedgerError(f"could not settle the ledger entry ({type(exc).__name__})") from exc
