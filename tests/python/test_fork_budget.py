"""Story 17-6: the per-PR fork ledger, the rescan allowance and the fail-closed run ceiling (CAP-2)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from ai_pr_review.cli import _settle_fork_budget
from ai_pr_review.review.fork_budget import (
    CAP_RUNS,
    CAP_USD,
    ENTRY_TRAILER,
    Caps,
    ForkBudget,
    ForkBudgetPause,
    LedgerError,
    allowance_of,
    cents_from_units,
    format_usd,
    is_entry_body,
    is_rescan_comment,
    parse_entry,
    render_entry,
    try_parse_entry,
    with_run_pointer,
)
from ai_pr_review.review.fork_gate import ForkGateFacts
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder, sanitize_outbound

# --- an in-memory backend ----------------------------------------------------


class Backend:
    """Comments of one pull request. ``bot`` holds the bot-authored ledger entries by ID."""

    def __init__(self) -> None:
        self.next_id = 100
        self.bot: dict[int, str] = {}
        self.maintainer: list[tuple[int, str]] = []
        self.fail_read = self.fail_create = self.fail_update = self.fail_delete = False
        self.before_create: Callable[[], None] | None = None
        self.after_create: Callable[[], None] | None = None
        self.lock = threading.Lock()

    def add(self, runs: int = 1, cents: int = 0, allowance: int = 0, rescan_id: int | None = None) -> int:
        with self.lock:
            self.next_id += 1
            self.bot[self.next_id] = render_entry(runs, cents, allowance, rescan_id)
            return self.next_id

    def list_budget_comments(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        if self.fail_read:
            raise RuntimeError("HTTP 500")
        with self.lock:
            return sorted(self.bot.items()), list(self.maintainer)

    def create_budget_entry(self, body: str) -> int:
        if self.before_create is not None:
            hook, self.before_create = self.before_create, None
            hook()
        if self.fail_create:
            raise RuntimeError("HTTP 500")
        with self.lock:
            self.next_id += 1
            self.bot[self.next_id] = body
            new_id = self.next_id
        if self.after_create is not None:
            hook, self.after_create = self.after_create, None
            hook()
        return new_id

    def update_budget_entry(self, comment_id: int, body: str) -> None:
        if self.fail_update:
            raise RuntimeError("HTTP 500")
        self.bot[comment_id] = body

    def delete_budget_entry(self, comment_id: int) -> None:
        if self.fail_delete:
            raise RuntimeError("HTTP 500")
        self.bot.pop(comment_id, None)

    def totals(self) -> tuple[int, int]:
        entries = [parse_entry(i, b) for i, b in self.bot.items()]
        return sum(e.runs for e in entries), sum(e.cents for e in entries)


def _budget(backend: Backend, **caps: int) -> ForkBudget:
    return ForkBudget(backend, Caps(**caps))  # type: ignore[arg-type]


RESCAN = "/ai-pr-review rescan"

# --- reserve, settle, cancel -------------------------------------------------


def test_reserve_then_settle_replaces_the_estimate_with_the_actual_cost() -> None:
    backend = Backend()
    budget = _budget(backend)
    budget.precheck()
    reservation = budget.reserve(40)
    assert backend.totals() == (1, 40)
    budget.settle(reservation, 12)
    assert backend.totals() == (1, 12)
    assert render_entry(1, 12, 0) == backend.bot[reservation.comment_id]


def test_a_cancelled_run_stays_charged_at_its_estimate() -> None:
    backend = Backend()
    for _ in range(3):
        _budget(backend).reserve(100)  # three runs that never settle
    assert backend.totals() == (3, 300)
    last = _budget(backend).reserve(100)
    assert last.estimate_cents == 100
    with pytest.raises(ForkBudgetPause) as stop:  # the cancelled runs still count, 4 of 4 used
        _budget(backend).precheck()
    assert stop.value.cap == CAP_RUNS


def test_settle_failure_raises_and_leaves_the_estimate_charged() -> None:
    backend = Backend()
    budget = _budget(backend)
    reservation = budget.reserve(40)
    backend.fail_update = True
    with pytest.raises(LedgerError):
        budget.settle(reservation, 5)
    assert backend.totals() == (1, 40)


# --- caps and pause text -----------------------------------------------------


def test_the_run_cap_pauses_the_precheck_and_the_text_names_the_cap_and_the_resume_command() -> None:
    backend = Backend()
    for _ in range(4):
        backend.add(cents=10)
    with pytest.raises(ForkBudgetPause) as stop:
        _budget(backend).precheck()
    assert stop.value.cap == CAP_RUNS
    assert "4 review runs" in stop.value.reason and "/ai-pr-review rescan" in stop.value.reason


def test_the_usd_cap_names_6_usd_and_nothing_is_written_when_the_estimate_does_not_fit() -> None:
    backend = Backend()
    backend.add(cents=590)
    budget = _budget(backend)
    budget.precheck()  # 5.90 of 6.00 is not at the cap yet
    with pytest.raises(ForkBudgetPause) as stop:
        budget.reserve(30)
    assert stop.value.cap == CAP_USD
    assert "6.00 USD" in stop.value.reason and "/ai-pr-review rescan" in stop.value.reason
    assert backend.totals() == (1, 590)  # a paused run appends nothing


def test_a_ledger_exactly_at_the_usd_cap_pauses_the_precheck() -> None:
    backend = Backend()
    backend.add(cents=600)
    with pytest.raises(ForkBudgetPause) as stop:
        _budget(backend).precheck()
    assert stop.value.cap == CAP_USD


def test_an_estimate_that_fits_exactly_is_admitted() -> None:
    backend = Backend()
    backend.add(cents=500)
    assert _budget(backend).reserve(100).estimate_cents == 100


# --- concurrent runs ---------------------------------------------------------


def test_two_concurrent_runs_cannot_both_pass_an_explicit_interleaving() -> None:
    """Both runs read, both append, then both re-read. Exactly one may proceed."""
    backend = Backend()
    for _ in range(3):
        backend.add(cents=10)  # room for exactly one more run
    read_barrier, write_barrier = threading.Barrier(2, timeout=10), threading.Barrier(2, timeout=10)
    seen: dict[str, int] = {"read": 0, "write": 0}
    first_reads: set[int] = set()
    first_writes: set[int] = set()
    real_list, real_create = backend.list_budget_comments, backend.create_budget_entry

    def list_comments() -> Any:
        me = threading.get_ident()
        result = real_list()
        if me not in first_reads:
            first_reads.add(me)
            read_barrier.wait()  # both runs have read before either one writes
        return result

    def create(body: str) -> int:
        me = threading.get_ident()
        new_id = real_create(body)
        if me not in first_writes:
            first_writes.add(me)
            write_barrier.wait()  # both entries exist before either run re-reads
        return new_id

    backend.list_budget_comments = list_comments  # type: ignore[method-assign]
    backend.create_budget_entry = create  # type: ignore[method-assign]
    outcomes: list[str] = []

    def run() -> None:
        try:
            _budget(backend).reserve(10)
            outcomes.append("pass")
        except ForkBudgetPause:
            outcomes.append("pause")

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(outcomes) == ["pass", "pause"], seen
    assert backend.totals() == (4, 40)  # the loser removed its own entry


def test_a_run_that_loses_the_order_deletes_its_own_entry_for_the_usd_cap_too() -> None:
    backend = Backend()
    backend.add(cents=300)
    backend.before_create = lambda: backend.add(cents=250)  # a competitor appends first
    with pytest.raises(ForkBudgetPause) as stop:
        _budget(backend).reserve(100)
    assert stop.value.cap == CAP_USD
    assert backend.totals() == (2, 550)  # own entry deleted, the competitor stays


def test_a_later_entry_never_counts_against_an_earlier_one() -> None:
    """Admission is ordered by comment ID. Entries after the own entry are their own problem."""
    backend = Backend()
    for _ in range(3):
        backend.add(cents=10)
    real_create = backend.create_budget_entry

    def create_then_a_later_run_appends(body: str) -> int:
        own = real_create(body)
        backend.add(cents=10)  # a later run, 5 entries now
        return own

    backend.create_budget_entry = create_then_a_later_run_appends  # type: ignore[method-assign]
    assert _budget(backend).reserve(10).estimate_cents == 10


def test_a_loser_that_cannot_delete_its_entry_still_pauses_and_stays_charged() -> None:
    backend = Backend()
    for _ in range(3):
        backend.add(cents=10)
    backend.before_create = lambda: backend.add(cents=10)
    backend.fail_delete = True
    with pytest.raises(ForkBudgetPause):
        _budget(backend).reserve(10)
    assert backend.totals() == (5, 50)


def test_a_missing_own_entry_after_the_write_pauses_the_run() -> None:
    backend = Backend()
    real_create = backend.create_budget_entry

    def create_then_lose(body: str) -> int:
        new_id = real_create(body)
        backend.bot.pop(new_id)  # a user with write access deleted it
        return new_id

    backend.create_budget_entry = create_then_lose  # type: ignore[method-assign]
    with pytest.raises(LedgerError):
        _budget(backend).reserve(10)


# --- the rescan allowance ----------------------------------------------------


def test_a_maintainer_rescan_adds_exactly_one_run_and_does_not_reset_the_ledger() -> None:
    backend = Backend()
    for _ in range(4):
        backend.add(cents=100)
    backend.maintainer = [(900, RESCAN)]
    budget = _budget(backend)
    budget.precheck()  # credited, so 4 of 5 runs
    assert allowance_of([parse_entry(i, b) for i, b in backend.bot.items()]) == 1
    budget.reserve(100)  # the fifth run
    assert backend.totals() == (5, 500)  # the first four entries are still charged
    with pytest.raises(ForkBudgetPause) as stop:  # a second run in the same window is paused again
        _budget(backend).precheck()
    assert stop.value.cap == CAP_RUNS and "5 review runs" in stop.value.reason


def test_a_second_run_or_a_job_rerun_does_not_credit_the_same_rescan_comment_again() -> None:
    backend = Backend()
    backend.add(cents=10)
    backend.maintainer = [(900, RESCAN)]
    for _ in range(3):
        _budget(backend).precheck()
    credits = [parse_entry(i, b) for i, b in backend.bot.items()]
    assert len([c for c in credits if c.rescan_id == 900]) == 1
    assert allowance_of(credits) == 1


def test_duplicate_credit_entries_for_one_comment_count_once() -> None:
    backend = Backend()
    backend.add(runs=0, allowance=1, rescan_id=900)
    backend.add(runs=0, allowance=1, rescan_id=900)  # a creation race
    assert allowance_of([parse_entry(i, b) for i, b in backend.bot.items()]) == 1


def test_one_allowance_unit_also_extends_the_usd_cap() -> None:
    backend = Backend()
    backend.add(cents=600)
    backend.maintainer = [(900, RESCAN)]
    budget = _budget(backend)
    budget.precheck()  # 6.00 used of 8.00
    assert budget.reserve(100).estimate_cents == 100
    with pytest.raises(ForkBudgetPause) as stop:
        budget.reserve(150)  # 7.00 + 1.50 passes 8.00
    assert stop.value.cap == CAP_USD and "8.00 USD" in stop.value.reason


def test_rescan_comments_older_than_the_first_reservation_earn_no_credit() -> None:
    backend = Backend()
    backend.maintainer = [(50, RESCAN), (60, RESCAN)]
    _budget(backend).precheck()  # no entry yet, so there is nothing to resume
    assert backend.bot == {}
    first = backend.add(cents=10)
    backend.maintainer = [(first - 1, RESCAN)]
    _budget(backend).precheck()
    assert len(backend.bot) == 1  # the old comment is still ignored


def test_a_rescan_after_the_pause_is_credited_exactly_once() -> None:
    backend = Backend()
    for _ in range(4):
        backend.add(cents=10)
    old = [(50, RESCAN)]
    backend.maintainer = old
    with pytest.raises(ForkBudgetPause):
        _budget(backend).precheck()  # the old comment does not resume the pull request
    backend.maintainer = old + [(900, RESCAN)]  # posted after the pause
    for _ in range(3):
        with contextlib.suppress(ForkBudgetPause):
            _budget(backend).precheck()
    credits = [parse_entry(i, b) for i, b in backend.bot.items() if "rescan" in b]
    assert [c.rescan_id for c in credits] == [900]


def test_the_pause_text_names_who_can_resume_correctly() -> None:
    backend = Backend()
    for _ in range(4):
        backend.add(cents=10)
    with pytest.raises(ForkBudgetPause) as stop:
        _budget(backend).precheck()
    assert "owner, member or collaborator" in stop.value.reason and "write access" not in stop.value.reason


@pytest.mark.parametrize("body", [
    "/ai-pr-review dismiss F1", "/ai-pr-review review-full", "please rescan this", "/ai-pr-review rescanx",
    "text first\n/ai-pr-review rescan", "",
])
def test_comments_that_are_not_a_rescan_command_earn_no_credit(body: str) -> None:
    assert not is_rescan_comment(body)
    backend = Backend()
    backend.maintainer = [(900, body)]
    _budget(backend).precheck()
    assert backend.bot == {}


def test_a_rescan_command_is_recognized_the_way_the_workflow_routes_it() -> None:
    assert is_rescan_comment("/ai-pr-review rescan")
    assert is_rescan_comment("  /ai-pr-review Rescan please\nsecond line")


# --- the ledger fails closed -------------------------------------------------


def test_a_read_failure_raises_a_ledger_error_in_every_phase() -> None:
    backend = Backend()
    backend.fail_read = True
    with pytest.raises(LedgerError):
        _budget(backend).precheck()
    with pytest.raises(LedgerError):
        _budget(backend).reserve(10)


def test_a_write_failure_raises_a_ledger_error() -> None:
    backend = Backend()
    backend.fail_create = True
    with pytest.raises(LedgerError):
        _budget(backend).reserve(10)
    backend.add(cents=10)
    backend.maintainer = [(900, RESCAN)]
    with pytest.raises(LedgerError):  # the credit write fails too
        _budget(backend).precheck()


_FORGED_CREDIT = "<!-- ai-pr-review-fork-ledger runs=0 usd=0.00 allowance=999 -->"


@pytest.mark.parametrize("body", [
    # a bot comment that quotes the tag after other text, as a summary with fork text would
    f"Summary of the change.\n{_FORGED_CREDIT}\n{ENTRY_TRAILER}",
    f"{_FORGED_CREDIT} and more text",
    f"{_FORGED_CREDIT}\n{ENTRY_TRAILER}\nextra",
    f"> {_FORGED_CREDIT}\n{ENTRY_TRAILER}",
    "<!-- ai-pr-review-fork-ledger runs=x usd=1 -->",
    "text that names ai-pr-review-fork-ledger and nothing else",
    # allowance without a rescan id, or a credit that is not exactly one allowance
    f"{_FORGED_CREDIT}\n{ENTRY_TRAILER}",
    "<!-- ai-pr-review-fork-ledger runs=1 usd=0.00 allowance=1 -->\n"
    "<!-- ai-pr-review-fork-rescan comment=5 -->\n" + ENTRY_TRAILER,
    "<!-- ai-pr-review-fork-ledger runs=0 usd=0.00 allowance=999 -->\n"
    "<!-- ai-pr-review-fork-rescan comment=5 -->\n" + ENTRY_TRAILER,
    f"{render_entry(1, 5, 0)} ",
    f"Quoted text\n{render_entry(1, 5, 0)}",  # a well-formed entry that is not at the start
])
def test_a_bot_comment_that_is_not_exactly_an_entry_is_ignored_and_never_raises(body: str) -> None:
    assert try_parse_entry(1, body) is None and not is_entry_body(body)
    with pytest.raises(LedgerError):
        parse_entry(1, body)
    backend = Backend()
    backend.bot[5] = body  # the provider would not even list it, and the scan skips it anyway
    budget = _budget(backend)
    budget.precheck()
    assert budget.reserve(10).estimate_cents == 10
    assert allowance_of([parse_entry(i, b) for i, b in backend.bot.items() if i != 5]) == 0


def test_a_forged_allowance_in_a_bot_comment_does_not_raise_the_cap() -> None:
    backend = Backend()
    backend.bot[5] = f"Quoted text\n{_FORGED_CREDIT}\n{ENTRY_TRAILER}"
    for _ in range(4):
        backend.add(cents=10)
    with pytest.raises(ForkBudgetPause):
        _budget(backend).precheck()


def test_the_provider_lists_only_exact_entries_from_the_bot() -> None:
    prov = _provider(_comments(
        {"id": 1, "user": _BOT, "body": f"Summary\n{_FORGED_CREDIT}\n{ENTRY_TRAILER}"},
        {"id": 2, "user": _BOT, "body": _FORGED_CREDIT},
        {"id": 3, "user": _BOT, "body": render_entry(1, 40, 0)},
    ))
    ledger, _ = prov.list_budget_comments()
    assert [cid for cid, _b in ledger] == [3]
    ForkBudget(prov, Caps()).precheck()


def test_the_entry_marker_round_trips_and_survives_the_outbound_scrub() -> None:
    body = render_entry(1, 1234, 0)
    assert "<!-- ai-pr-review-fork-ledger runs=1 usd=12.34 allowance=0 -->" in body
    entry = parse_entry(7, body)
    assert (entry.runs, entry.cents, entry.allowance, entry.rescan_id) == (1, 1234, 0, None)
    credit = render_entry(0, 0, 1, 900)
    assert sanitize_outbound({"body": body})["body"] == body
    assert sanitize_outbound({"body": credit})["body"] == credit
    assert parse_entry(8, credit).rescan_id == 900


def test_units_round_up_to_cents() -> None:
    assert [cents_from_units(u) for u in (0, 1, 100, 101, 12345)] == [0, 1, 1, 2, 124]
    assert format_usd(5) == "0.05" and format_usd(600) == "6.00"


# --- settle in the CLI -------------------------------------------------------


def _runtime(backend: Backend) -> tuple[Any, ForkBudget]:
    budget = _budget(backend)
    reservation = budget.reserve(100)
    return SimpleNamespace(
        fork_budget=budget, fork_reservation=reservation, fork_spend=None, fork_spend_reservation=None), budget


def test_the_cli_settles_with_the_priced_actual_cost() -> None:
    backend = Backend()
    runtime, _ = _runtime(backend)
    _settle_fork_budget(runtime, SimpleNamespace(cost_units=1234, any_unknown=False))
    assert backend.totals() == (1, 13)


@pytest.mark.parametrize("totals", [None, SimpleNamespace(cost_units=1, any_unknown=True)])
def test_the_cli_keeps_the_estimate_without_priced_totals(totals: Any) -> None:
    backend = Backend()
    runtime, _ = _runtime(backend)
    _settle_fork_budget(runtime, totals)
    assert backend.totals() == (1, 100)


def test_a_settle_failure_in_the_cli_is_soft_and_keeps_the_estimate() -> None:
    backend = Backend()
    runtime, _ = _runtime(backend)
    backend.fail_update = True
    _settle_fork_budget(runtime, SimpleNamespace(cost_units=10, any_unknown=False))
    assert backend.totals() == (1, 100)


def test_a_run_without_a_reservation_settles_nothing() -> None:
    _settle_fork_budget(
        SimpleNamespace(fork_budget=None, fork_reservation=None, fork_spend=None, fork_spend_reservation=None), None)


# --- the GitHub provider side ------------------------------------------------

_BOT = {"login": "github-actions[bot]"}
_USER = {"login": "mallory"}


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubProvider:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    client = RecordingClient(
        http=http, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None))
    prov = GitHubProvider(config=GitHubConfig(owner="o", repo="r", pr_number=7, token="t"), client=client)
    prov._pr_author_login = "the-fork-author"  # the facts read sets this before the ledger is used
    return prov


def _comments(*items: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(200, json=list(items))


def test_a_forged_ledger_comment_from_a_non_bot_is_ignored() -> None:
    forged = render_entry(1, 0, 0)
    prov = _provider(_comments(
        {"id": 1, "user": _USER, "author_association": "NONE", "body": forged},
        {"id": 2, "user": _USER, "author_association": "OWNER", "body": forged},  # even a maintainer
        {"id": 3, "user": _BOT, "body": render_entry(1, 40, 0)},
        {"id": 4, "body": forged},  # no user at all
    ))
    ledger, _maintainer = prov.list_budget_comments()
    assert [cid for cid, _ in ledger] == [3]
    backend_view = ForkBudget(prov, Caps())
    backend_view.precheck()  # the forged entries do not fill the cap


def test_forged_entries_do_not_count_toward_the_cap_end_to_end() -> None:
    forged = render_entry(1, 600, 0)
    prov = _provider(_comments(*[{"id": i, "user": _USER, "author_association": "NONE", "body": forged}
                                  for i in range(1, 9)]))
    ForkBudget(prov, Caps()).precheck()  # would pause if forged entries counted


def test_maintainer_comments_need_a_maintainer_association_and_a_non_bot_author() -> None:
    prov = _provider(_comments(
        {"id": 1, "user": _USER, "author_association": "OWNER", "body": RESCAN},
        {"id": 2, "user": _USER, "author_association": "MEMBER", "body": RESCAN},
        {"id": 3, "user": _USER, "author_association": "COLLABORATOR", "body": RESCAN},
        {"id": 4, "user": _USER, "author_association": "CONTRIBUTOR", "body": RESCAN},
        {"id": 5, "user": _USER, "author_association": "NONE", "body": RESCAN},
        {"id": 6, "user": _USER, "body": RESCAN},
        {"id": 7, "user": _BOT, "author_association": "OWNER", "body": RESCAN},
    ))
    _ledger, maintainer = prov.list_budget_comments()
    assert [cid for cid, _ in maintainer] == [1, 2, 3]


def test_a_forged_rescan_from_a_stranger_earns_no_credit_end_to_end() -> None:
    created: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            created.append(req.content.decode())
            return httpx.Response(201, json={"id": 50})
        return httpx.Response(200, json=[{"id": 9, "user": _USER, "author_association": "NONE", "body": RESCAN}])

    ForkBudget(_provider(handler), Caps()).precheck()
    assert created == []


def test_a_failed_page_raises_instead_of_returning_a_partial_list() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json=[{"id": 1, "user": _BOT, "body": render_entry(1, 0, 0)}],
            headers={"link": '<https://api.github.com/repos/o/r/issues/7/comments?page=2>; rel="next"'})

    with pytest.raises(RuntimeError):
        _provider(handler).list_budget_comments()
    with pytest.raises(LedgerError):
        ForkBudget(_provider(handler), Caps()).precheck()


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"not": "a list"}),
    httpx.Response(200, json=[{"user": _BOT, "body": "no id"}]),
    httpx.Response(403, json={"message": "no"}),
])
def test_a_bad_listing_raises(response: httpx.Response) -> None:
    with pytest.raises(RuntimeError):
        _provider(lambda _req: response).list_budget_comments()


def test_create_update_and_delete_use_the_issue_comment_endpoints() -> None:
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(201 if req.method == "POST" else 200, json={"id": 77})

    prov = _provider(handler)
    assert prov.create_budget_entry(render_entry(1, 5, 0)) == 77
    prov.update_budget_entry(77, render_entry(1, 4, 0))
    prov.delete_budget_entry(77)
    assert calls == [
        ("POST", "/repos/o/r/issues/7/comments"), ("PATCH", "/repos/o/r/issues/comments/77"),
        ("DELETE", "/repos/o/r/issues/comments/77")]


@pytest.mark.parametrize("status", [403, 500])
def test_a_failed_write_raises(status: int) -> None:
    prov = _provider(lambda _req: httpx.Response(status, text="x"))
    with pytest.raises(RuntimeError):
        prov.create_budget_entry("b")
    with pytest.raises(RuntimeError):
        prov.update_budget_entry(1, "b")
    with pytest.raises(RuntimeError):
        prov.delete_budget_entry(1)


# --- the runtime wiring ------------------------------------------------------


def _make_provider_class() -> type:
    from tests.python.test_fork_spend import SpendMem
    from tests.python.test_runtime import _FakeProvider

    class _Provider(_FakeProvider):
        """The fake provider of test_runtime plus the gate facts and the ledger calls."""

        def __init__(self, backend: Backend, association: str = "NONE") -> None:
            super().__init__()
            self.backend, self.association = backend, association
            self.spend = SpendMem()

        def get_fork_gate_facts(self, head_sha: str, context: str, *, read_approval: bool = True) -> ForkGateFacts:
            return ForkGateFacts("mallory/r", "o/r", "", "mallory", self.association)

        def list_budget_comments(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
            return self.backend.list_budget_comments()

        def create_budget_entry(self, body: str) -> int:
            return self.backend.create_budget_entry(body)

        def update_budget_entry(self, comment_id: int, body: str) -> None:
            self.backend.update_budget_entry(comment_id, body)

        def delete_budget_entry(self, comment_id: int) -> None:
            self.backend.delete_budget_entry(comment_id)

        def list_spend_entries(self, since: str) -> list[tuple[int, str]]:
            return self.spend.list_spend_entries(since)

        def create_spend_entry(self, body: str) -> int:
            return self.spend.create_spend_entry(body)

        def update_spend_entry(self, comment_id: int, body: str) -> None:
            self.spend.update_spend_entry(comment_id, body)

        def delete_spend_entry(self, comment_id: int) -> None:
            self.spend.delete_spend_entry(comment_id)

        def list_open_pulls(self) -> list[tuple[int, str, str]]:
            return self.spend.list_open_pulls()

    return _Provider


def _Provider(backend: Backend, association: str = "NONE") -> Any:  # noqa: N802 - reads like a class
    return _make_provider_class()(backend, association)


async def _build(tmp_path: Path, provider: Any, **cfg: Any) -> Any:
    from ai_pr_review.review.runtime import build_review_runtime
    from tests.python.test_runtime import _make_config, _make_diff_result

    config = _make_config(
        event_name="pull_request_target", head_sha="a" * 40, fork_policy="auto", **cfg)
    with (
        patch("ai_pr_review.review.fork_gate.workspace_head", return_value="a" * 40),
        patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()) as compute,
        patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
        patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(tmp_path / "diff.txt")}, clear=False),
    ):
        result = await build_review_runtime(config, provider_factory=lambda: provider)  # type: ignore[arg-type]
    return result, compute


@pytest.mark.anyio
async def test_a_fork_run_reserves_before_any_llm_call_and_carries_the_reservation(tmp_path: Path) -> None:
    backend = Backend()
    runtime, _ = await _build(tmp_path, _Provider(backend))
    assert runtime.fork_profile is not None
    assert runtime.fork_reservation is not None and runtime.fork_budget is not None
    assert backend.totals()[0] == 1
    assert runtime.fork_reservation.estimate_cents == backend.totals()[1] > 0


@pytest.mark.anyio
async def test_a_fork_run_at_the_cap_pauses_before_compute_and_names_the_cap(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    backend = Backend()
    for _ in range(4):
        backend.add(cents=10)
    result, compute = await _build(tmp_path, _Provider(backend))
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and result.fork_output_clamp
    assert "4 review runs" in result.reason and "/ai-pr-review rescan" in result.reason
    compute.assert_not_called()  # no git diff and no analyzer on a paused run
    assert backend.totals() == (4, 40)


@pytest.mark.anyio
@pytest.mark.parametrize("fail", ["fail_read", "fail_create"])
async def test_a_ledger_failure_pauses_the_fork_run(tmp_path: Path, fail: str) -> None:
    from ai_pr_review.review.fork_budget import LEDGER_ERROR_TEXT
    from ai_pr_review.review.runtime import SkipPlan

    backend = Backend()
    setattr(backend, fail, True)
    result, _ = await _build(tmp_path, _Provider(backend))
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and result.fork_output_clamp
    assert result.reason == with_run_pointer(LEDGER_ERROR_TEXT)


@pytest.mark.anyio
async def test_a_failed_estimate_pauses_a_fork_run_and_reserves_nothing(tmp_path: Path) -> None:
    from ai_pr_review.review.fork_budget import ESTIMATE_ERROR_TEXT
    from ai_pr_review.review.runtime import SkipPlan

    backend = Backend()
    with patch("ai_pr_review.review.cost_ceiling.estimate_review_cost", side_effect=ValueError("corrupt")):
        result, _ = await _build(tmp_path, _Provider(backend))
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and result.fork_output_clamp
    assert result.reason == with_run_pointer(ESTIMATE_ERROR_TEXT)
    assert backend.bot == {}


@pytest.mark.anyio
async def test_a_fork_run_with_an_unpriced_model_pauses(tmp_path: Path) -> None:
    from ai_pr_review.review.fork_budget import ESTIMATE_ERROR_TEXT
    from ai_pr_review.review.runtime import SkipPlan

    backend = Backend()
    result, _ = await _build(tmp_path, _Provider(backend), model_standard="no-such-model-x")
    assert isinstance(result, SkipPlan) and result.reason == with_run_pointer(ESTIMATE_ERROR_TEXT) and backend.bot == {}


@pytest.mark.anyio
async def test_a_trusted_author_gets_the_normal_pipeline_with_no_ledger_call(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import ReviewRuntime

    backend = Backend()
    backend.fail_read = backend.fail_create = True  # any ledger call would pause the run
    runtime, _ = await _build(tmp_path, _Provider(backend, association="OWNER"))
    assert isinstance(runtime, ReviewRuntime)
    assert runtime.fork_profile is None and runtime.fork_budget is None and runtime.fork_reservation is None


@pytest.mark.anyio
async def test_a_dry_run_makes_no_ledger_call(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import ReviewRuntime

    backend = Backend()
    backend.fail_read = True
    runtime, _ = await _build(tmp_path, _Provider(backend), dry_run=True)
    assert isinstance(runtime, ReviewRuntime) and runtime.fork_reservation is None


@pytest.mark.anyio
async def test_a_fork_run_with_no_pricing_data_pauses(tmp_path: Path) -> None:
    from ai_pr_review.review.fork_budget import ESTIMATE_ERROR_TEXT
    from ai_pr_review.review.runtime import SkipPlan

    backend = Backend()
    with patch("ai_pr_review.pricing.load_pricing", return_value={}):
        result, _ = await _build(tmp_path, _Provider(backend))
    assert isinstance(result, SkipPlan) and result.reason == with_run_pointer(ESTIMATE_ERROR_TEXT) and backend.bot == {}


@pytest.mark.anyio
async def test_the_rescan_run_is_credited_and_then_proceeds(tmp_path: Path) -> None:
    backend = Backend()
    for _ in range(4):
        backend.add(cents=10)
    backend.maintainer = [(900, RESCAN)]
    runtime, _ = await _build(tmp_path, _Provider(backend))
    assert runtime.fork_reservation is not None
    assert backend.totals()[0] == 5


@pytest.mark.anyio
async def test_a_fork_profile_run_never_gets_more_than_the_fork_ceiling(tmp_path: Path) -> None:
    backend = Backend()
    runtime, _ = await _build(tmp_path, _Provider(backend), max_cost_usd=9.0)
    assert runtime.config.max_cost_usd == 2.0
    runtime, _ = await _build(tmp_path, _Provider(Backend()), max_cost_usd=1.5)
    assert runtime.config.max_cost_usd == 1.5  # a smaller explicit ceiling stays


@pytest.mark.anyio
@pytest.mark.parametrize("zero", [0.0, -1.0])
async def test_a_zero_fork_ceiling_never_removes_the_ceiling_at_runtime(tmp_path: Path, zero: float) -> None:
    from ai_pr_review.config import ReviewConfig
    from ai_pr_review.review.runtime import _apply_fork_gate

    config = ReviewConfig.model_construct(  # no validation, as a hand-built config would be
        **{**ReviewConfig.model_validate({"vcs_provider": "github", "provider": "anthropic"}).model_dump(),
           "event_name": "pull_request_target", "head_sha": "a" * 40, "fork_policy": "auto",
           "fork_max_cost_usd": zero, "max_cost_usd": 0.0})
    with patch("ai_pr_review.review.fork_gate.workspace_head", return_value="a" * 40):
        out, skip, profile = _apply_fork_gate(config, _Provider(Backend()))
    assert skip is None and profile is not None and out.max_cost_usd == 2.0


def test_a_zero_fork_ceiling_in_the_config_falls_back_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from ai_pr_review.config import ReviewConfig

    monkeypatch.setenv("AI_FORK_MAX_COST_USD", "0")
    assert ReviewConfig.from_env().fork_max_cost_usd == 2.0
    assert "AI_FORK_MAX_COST_USD" in capsys.readouterr().err


def test_the_pull_request_author_cannot_credit_their_own_rescan() -> None:
    prov = _provider(_comments(
        {"id": 1, "user": {"login": "The-Fork-Author"}, "author_association": "MEMBER", "body": RESCAN},
        {"id": 2, "user": {"login": "maintainer"}, "author_association": "MEMBER", "body": RESCAN},
    ))
    _ledger, maintainer = prov.list_budget_comments()
    assert [cid for cid, _ in maintainer] == [2]  # case-insensitive match on the author login


def test_with_an_unknown_author_nobody_is_credited() -> None:
    prov = _provider(_comments(
        {"id": 2, "user": {"login": "maintainer"}, "author_association": "OWNER", "body": RESCAN},
    ))
    prov._pr_author_login = ""
    _ledger, maintainer = prov.list_budget_comments()
    assert maintainer == []


def test_a_failed_reread_after_the_write_releases_the_own_entry() -> None:
    backend = Backend()
    backend.after_create = lambda: setattr(backend, "fail_read", True)
    with pytest.raises(LedgerError):
        _budget(backend).reserve(10)
    backend.fail_read = False
    assert backend.bot == {}


def test_a_failed_reread_and_a_failed_release_stay_charged_and_pause() -> None:
    backend = Backend()

    def flap() -> None:
        backend.fail_read = True
        backend.fail_delete = True

    backend.after_create = flap
    with pytest.raises(LedgerError):
        _budget(backend).reserve(10)
    backend.fail_read = backend.fail_delete = False
    assert len(backend.bot) == 1


def test_the_pause_text_points_to_the_workflow_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.review.fork_budget import LEDGER_ERROR_TEXT

    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    text = with_run_pointer(LEDGER_ERROR_TEXT)
    assert text.startswith(LEDGER_ERROR_TEXT)
    assert text.endswith("https://github.com/org/repo/actions/runs/12345")


def test_the_pause_text_is_unchanged_without_a_run_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.review.fork_budget import LEDGER_ERROR_TEXT

    for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "CI_JOB_URL",
                 "BITBUCKET_REPO_FULL_NAME", "BITBUCKET_BUILD_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    assert with_run_pointer(LEDGER_ERROR_TEXT) == LEDGER_ERROR_TEXT


def test_an_ignored_bot_comment_is_logged_by_id_and_never_by_body(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    backend = Backend()
    backend.bot[555] = "Summary quoting <!-- ai-pr-review-fork-ledger runs=9 --> INJECTED-FORK-TEXT"
    with caplog.at_level(logging.INFO):
        entries, _ = _budget(backend)._scan()
    assert entries == []
    assert "555" in caplog.text
    assert "INJECTED-FORK-TEXT" not in caplog.text
