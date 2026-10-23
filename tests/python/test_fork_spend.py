"""Story 17-7: the author cap and the global daily spend ledger (CAP-2)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from ai_pr_review.cli import _settle_fork_budget
from ai_pr_review.config import ReviewConfig
from ai_pr_review.review.fork_budget import (
    LEDGER_ERROR_TEXT,
    ForkBudgetPause,
    LedgerError,
    with_run_pointer,
)
from ai_pr_review.review.fork_gate import ForkGateFacts
from ai_pr_review.review.fork_spend import (
    AUTHOR_MAX_PRS,
    CAP_AUTHOR,
    CAP_DAILY,
    SPEND_ISSUE_TITLE,
    STATE_RESERVED,
    STATE_SETTLED,
    TIER_RESERVED,
    TIER_SHARED,
    DailyCaps,
    ForkSpend,
    check_author_cap,
    parse_spend_entry,
    render_spend_entry,
    tier_of,
)
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder, sanitize_outbound

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


# --- an in-memory backend ----------------------------------------------------


class SpendMem:
    """The spend issue comments and the open pull request list. ``bot`` holds bot comments by ID."""

    def __init__(self) -> None:
        self.next_id = 100
        self.bot: dict[int, str] = {}
        self.pulls: list[tuple[int, str, str]] = [(42, "mallory", "mallory/r")]
        self.fail_read = self.fail_create = self.fail_update = self.fail_delete = self.fail_pulls = False
        self.before_create: Callable[[], None] | None = None
        self.after_create: Callable[[], None] | None = None
        self.since: list[str] = []
        self.creates = 0
        self.lock = threading.Lock()

    def add(self, cents: int, tier: str = TIER_SHARED, *, age: timedelta = timedelta(0),
            state: str = STATE_RESERVED, pr: int = 1, now: datetime = NOW) -> int:
        with self.lock:
            self.next_id += 1
            self.bot[self.next_id] = render_spend_entry(tier, "9", pr, cents, state, now - age)
            return self.next_id

    def list_spend_entries(self, since: str) -> list[tuple[int, str]]:
        self.since.append(since)
        if self.fail_read:
            raise RuntimeError("HTTP 500")
        with self.lock:
            return sorted(self.bot.items())

    def create_spend_entry(self, body: str) -> int:
        if self.before_create is not None:
            hook, self.before_create = self.before_create, None
            hook()
        if self.fail_create:
            raise RuntimeError("HTTP 403")
        with self.lock:
            self.next_id += 1
            self.bot[self.next_id] = body
            self.creates += 1
            new_id = self.next_id
        if self.after_create is not None:
            hook, self.after_create = self.after_create, None
            hook()
        return new_id

    def update_spend_entry(self, comment_id: int, body: str) -> None:
        if self.fail_update:
            raise RuntimeError("HTTP 500")
        self.bot[comment_id] = body

    def delete_spend_entry(self, comment_id: int) -> None:
        if self.fail_delete:
            raise RuntimeError("HTTP 500")
        self.bot.pop(comment_id, None)

    def list_open_pulls(self) -> list[tuple[int, str, str]]:
        if self.fail_pulls:
            raise RuntimeError("HTTP 500")
        return list(self.pulls)

    def sum(self, tier: str) -> int:
        return sum(parse_spend_entry(i, b).cents for i, b in self.bot.items() if parse_spend_entry(i, b).tier == tier)


def _spend(mem: SpendMem, tier: str = TIER_SHARED, **caps: Any) -> ForkSpend:
    return ForkSpend(mem, DailyCaps(**caps), tier=tier, pr_number=42, run_id="555", clock=lambda: NOW)


# --- the tier split ----------------------------------------------------------


def test_the_pools_are_a_strict_70_30_partition_of_50_usd() -> None:
    caps = DailyCaps()
    assert (caps.pool_cents(TIER_RESERVED), caps.pool_cents(TIER_SHARED)) == (3500, 1500)
    odd = DailyCaps.from_usd(10.01, 0.7)
    assert odd.pool_cents(TIER_RESERVED) + odd.pool_cents(TIER_SHARED) == 1001


@pytest.mark.parametrize(("association", "tier"), [
    ("CONTRIBUTOR", TIER_RESERVED), ("contributor", TIER_RESERVED),
    ("FIRST_TIME_CONTRIBUTOR", TIER_SHARED), ("FIRST_TIMER", TIER_SHARED), ("NONE", TIER_SHARED),
    ("MEMBER", TIER_SHARED), ("", TIER_SHARED), ("SOMETHING_NEW", TIER_SHARED),
])
def test_only_contributor_has_the_reserved_pool(association: str, tier: str) -> None:
    assert tier_of(association) == tier


def test_exhausting_the_shared_pool_does_not_touch_the_reserved_pool() -> None:
    mem = SpendMem()
    mem.add(1500, TIER_SHARED)  # the whole 30 percent
    with pytest.raises(ForkBudgetPause) as stop:
        _spend(mem, TIER_SHARED).precheck()
    assert stop.value.cap == CAP_DAILY
    with pytest.raises(ForkBudgetPause):
        _spend(mem, TIER_SHARED).reserve(1)
    _spend(mem, TIER_RESERVED).precheck()
    reservation = _spend(mem, TIER_RESERVED).reserve(3500)  # the whole 70 percent is still free
    assert reservation.estimate_cents == 3500
    assert (mem.sum(TIER_RESERVED), mem.sum(TIER_SHARED)) == (3500, 1500)


def test_exhausting_the_reserved_pool_does_not_touch_the_shared_pool() -> None:
    mem = SpendMem()
    mem.add(3500, TIER_RESERVED)
    with pytest.raises(ForkBudgetPause):
        _spend(mem, TIER_RESERVED).reserve(1)
    assert _spend(mem, TIER_SHARED).reserve(1500).estimate_cents == 1500


def test_the_shared_pool_is_shared_between_associations_and_pr_numbers() -> None:
    mem = SpendMem()
    mem.add(900, TIER_SHARED, pr=3)
    mem.add(600, TIER_SHARED, pr=4)
    with pytest.raises(ForkBudgetPause):
        _spend(mem, tier_of("FIRST_TIMER")).precheck()


def test_the_pause_text_names_the_daily_cap_and_says_it_clears_with_the_window() -> None:
    mem = SpendMem()
    mem.add(1500, TIER_SHARED)
    with pytest.raises(ForkBudgetPause) as stop:
        _spend(mem).precheck()
    text = stop.value.reason
    assert "daily" in text and "50.00 USD" in text and "15.00 USD" in text
    assert "clears as the 24 hour window moves on" in text and "rescan does not change" in text
    assert "—" not in text and ";" not in text


# --- reserve, settle, cancel -------------------------------------------------


def test_reserve_then_settle_replaces_the_estimate_and_keeps_the_timestamp() -> None:
    mem = SpendMem()
    spend = _spend(mem)
    spend.precheck()
    reservation = spend.reserve(40)
    entry = parse_spend_entry(reservation.comment_id, mem.bot[reservation.comment_id])
    assert (entry.cents, entry.state, entry.tier, entry.pr, entry.ts) == (40, STATE_RESERVED, TIER_SHARED, 42, NOW)
    assert "run=555" in mem.bot[reservation.comment_id]
    spend.settle(reservation, 12)
    entry = parse_spend_entry(reservation.comment_id, mem.bot[reservation.comment_id])
    assert (entry.cents, entry.state, entry.ts) == (12, STATE_SETTLED, NOW)


def test_a_cancelled_run_stays_charged_at_its_estimate() -> None:
    mem = SpendMem()
    _spend(mem).reserve(1000)  # never settled
    with pytest.raises(ForkBudgetPause):
        _spend(mem).reserve(600)
    assert mem.sum(TIER_SHARED) == 1000


def test_a_settle_failure_raises_and_leaves_the_estimate_charged() -> None:
    mem = SpendMem()
    spend = _spend(mem)
    reservation = spend.reserve(40)
    mem.fail_update = True
    with pytest.raises(LedgerError):
        spend.settle(reservation, 5)
    assert mem.sum(TIER_SHARED) == 40


def test_an_estimate_that_does_not_fit_appends_nothing() -> None:
    mem = SpendMem()
    mem.add(1480, TIER_SHARED)
    with pytest.raises(ForkBudgetPause):
        _spend(mem).reserve(30)
    assert mem.creates == 0 and len(mem.bot) == 1


def test_an_estimate_that_fits_exactly_is_admitted_and_a_full_pool_pauses_the_precheck() -> None:
    mem = SpendMem()
    mem.add(1470, TIER_SHARED)
    _spend(mem).reserve(30)
    with pytest.raises(ForkBudgetPause):
        _spend(mem).precheck()  # at the cap exactly


def test_a_zero_daily_cap_pauses_every_run_and_writes_nothing() -> None:
    mem = SpendMem()
    for tier in (TIER_SHARED, TIER_RESERVED):
        with pytest.raises(ForkBudgetPause):
            _spend(mem, tier, max_cents=0).precheck()
        with pytest.raises(ForkBudgetPause):
            _spend(mem, tier, max_cents=0).reserve(0)
    assert mem.creates == 0


# --- concurrent runs ---------------------------------------------------------


def test_two_concurrent_runs_cannot_both_pass_an_explicit_interleaving() -> None:
    """Both runs read, both append, then both re-read. Exactly one may proceed."""
    mem = SpendMem()
    mem.add(500, TIER_SHARED)  # room for exactly one run of 1000
    read_barrier, write_barrier = threading.Barrier(2, timeout=10), threading.Barrier(2, timeout=10)
    first_reads: set[int] = set()
    first_writes: set[int] = set()
    real_list, real_create = mem.list_spend_entries, mem.create_spend_entry

    def list_entries(since: str) -> Any:
        me = threading.get_ident()
        result = real_list(since)
        if me not in first_reads:
            first_reads.add(me)
            read_barrier.wait()
        return result

    def create(body: str) -> int:
        me = threading.get_ident()
        new_id = real_create(body)
        if me not in first_writes:
            first_writes.add(me)
            write_barrier.wait()
        return new_id

    mem.list_spend_entries = list_entries  # type: ignore[method-assign]
    mem.create_spend_entry = create  # type: ignore[method-assign]
    outcomes: list[str] = []

    def run() -> None:
        try:
            _spend(mem).reserve(1000)
            outcomes.append("pass")
        except ForkBudgetPause:
            outcomes.append("pause")

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(outcomes) == ["pass", "pause"]
    assert mem.sum(TIER_SHARED) == 1500  # the loser removed its own entry


def test_a_competitor_that_appends_first_wins_and_the_later_run_removes_its_entry() -> None:
    mem = SpendMem()
    mem.before_create = lambda: mem.add(1000, TIER_SHARED)
    with pytest.raises(ForkBudgetPause) as stop:
        _spend(mem).reserve(1000)
    assert stop.value.cap == CAP_DAILY
    assert len(mem.bot) == 1 and mem.sum(TIER_SHARED) == 1000


def test_a_later_entry_never_counts_against_an_earlier_one() -> None:
    mem = SpendMem()
    mem.after_create = lambda: mem.add(1000, TIER_SHARED)  # a later run appends before the re-read
    assert _spend(mem).reserve(1000).estimate_cents == 1000


def test_a_loser_that_cannot_delete_its_entry_still_pauses_and_stays_charged() -> None:
    mem = SpendMem()
    mem.before_create = lambda: mem.add(1000, TIER_SHARED)
    mem.fail_delete = True
    with pytest.raises(ForkBudgetPause):
        _spend(mem).reserve(1000)
    assert mem.sum(TIER_SHARED) == 2000


def test_a_missing_own_entry_after_the_write_pauses_the_run() -> None:
    mem = SpendMem()
    mem.after_create = lambda: mem.bot.clear()  # a user with write access removed it
    with pytest.raises(LedgerError):
        _spend(mem).reserve(10)


def test_release_deletes_the_own_entry_and_a_failed_release_is_soft() -> None:
    mem = SpendMem()
    spend = _spend(mem)
    reservation = spend.reserve(10)
    mem.fail_delete = True
    spend.release(reservation)  # no raise
    assert mem.sum(TIER_SHARED) == 10
    mem.fail_delete = False
    spend.release(reservation)
    assert mem.bot == {}


# --- the 24 hour window ------------------------------------------------------


def test_only_entries_of_the_last_24_hours_count() -> None:
    mem = SpendMem()
    mem.add(1500, TIER_SHARED, age=timedelta(hours=25))
    _spend(mem).precheck()  # the old entry does not count
    mem.add(1500, TIER_SHARED, age=timedelta(hours=23))
    with pytest.raises(ForkBudgetPause):
        _spend(mem).precheck()


def test_the_since_filter_is_the_window_start_minus_the_skew_margin() -> None:
    mem = SpendMem()
    _spend(mem).precheck()
    assert mem.since == ["2026-10-20T11:55:00Z"]


def test_a_settle_edit_does_not_move_an_entry_into_the_window() -> None:
    mem = SpendMem()
    old = mem.add(1500, TIER_SHARED, age=timedelta(hours=26), state=STATE_SETTLED)  # edited just now, listed
    assert old in mem.bot
    _spend(mem).reserve(1500)  # the marker ts, not the edit time, decides


# --- fail closed -------------------------------------------------------------


@pytest.mark.parametrize("phase", ["precheck", "reserve"])
def test_a_read_failure_raises_a_ledger_error(phase: str) -> None:
    mem = SpendMem()
    mem.fail_read = True
    with pytest.raises(LedgerError):
        getattr(_spend(mem), phase)(*(() if phase == "precheck" else (10,)))


def test_a_write_failure_raises_a_ledger_error_and_leaves_nothing() -> None:
    mem = SpendMem()
    mem.fail_create = True
    with pytest.raises(LedgerError, match="could not write"):
        _spend(mem).reserve(10)
    assert mem.bot == {}


@pytest.mark.parametrize("body", [
    "<!-- ai-pr-review-fork-spend tier=shared -->",
    "<!-- ai-pr-review-fork-spend tier=weird run=1 pr=1 usd=0.10 state=reserved ts=2026-10-21T12:00:00Z -->",
    "<!-- ai-pr-review-fork-spend tier=shared run=1 pr=1 usd=0.10 state=reserved ts=2026-13-45T99:00:00Z -->",
    "ai-pr-review-fork-spend plain text",
])
def test_a_bot_entry_with_a_broken_marker_pauses_instead_of_being_skipped(body: str) -> None:
    mem = SpendMem()
    mem.bot[5] = body
    with pytest.raises(LedgerError):
        _spend(mem).precheck()


def test_the_entry_marker_round_trips_and_survives_the_outbound_scrub() -> None:
    body = render_spend_entry(TIER_RESERVED, "123456", 42, 1234, STATE_RESERVED, NOW)
    assert ("<!-- ai-pr-review-fork-spend tier=reserved run=123456 pr=42 usd=12.34 "
            "state=reserved ts=2026-10-21T12:00:00Z -->") in body
    entry = parse_spend_entry(7, body)
    assert (entry.tier, entry.pr, entry.cents, entry.state, entry.ts) == (TIER_RESERVED, 42, 1234, "reserved", NOW)
    assert sanitize_outbound({"body": body})["body"] == body
    assert "run=none" in render_spend_entry(TIER_SHARED, "not a number", 1, 1, STATE_RESERVED, NOW)


# --- the author cap ----------------------------------------------------------


def _cap(pulls: list[tuple[int, str, str]], pr: int, author: str = "mallory") -> None:
    mem = SpendMem()
    mem.pulls = pulls
    check_author_cap(mem, pr_number=pr, author=author, base_repo="o/r")


def _forks(*numbers: int, author: str = "mallory") -> list[tuple[int, str, str]]:
    return [(n, author, f"{author}/r") for n in numbers]


def test_the_two_lowest_numbered_open_fork_prs_of_an_author_are_reviewed() -> None:
    assert AUTHOR_MAX_PRS == 2
    _cap(_forks(10, 11), 10)
    _cap(_forks(10, 11), 11)
    _cap(_forks(12, 10, 11), 11)  # the order of the listing does not matter


def test_the_third_open_fork_pr_pauses_and_the_text_names_the_cap() -> None:
    with pytest.raises(ForkBudgetPause) as stop:
        _cap(_forks(10, 11, 12), 12)
    assert stop.value.cap == CAP_AUTHOR
    assert "2 open fork pull requests" in stop.value.reason and "`mallory`" in stop.value.reason
    assert "3 open fork pull requests" in stop.value.reason
    with pytest.raises(ForkBudgetPause):
        _cap(_forks(10, 11, 12, 13), 13)


def test_closing_a_lower_pr_lets_the_third_proceed() -> None:
    _cap(_forks(11, 12), 12)  # #10 closed, #12 is now the second lowest


def test_prs_of_other_authors_and_same_repository_prs_do_not_count() -> None:
    pulls = _forks(10, 11) + _forks(1, 2, 3, author="alice") + [(4, "mallory", "o/r"), (5, "mallory", "O/R")]
    _cap(pulls, 11)
    with pytest.raises(ForkBudgetPause):
        _cap(_forks(10, 11) + [(12, "mallory", "")], 12)  # a deleted head repository counts as a fork


def test_the_author_match_ignores_case_and_the_fork_owner() -> None:
    pulls = [(10, "Mallory", "org-fork/r"), (11, "MALLORY", "mallory/r"), (12, "mallory", "third/r")]
    with pytest.raises(ForkBudgetPause):
        _cap(pulls, 12)


def test_the_author_cap_fails_closed() -> None:
    mem = SpendMem()
    mem.fail_pulls = True
    with pytest.raises(LedgerError):
        check_author_cap(mem, pr_number=42, author="mallory", base_repo="o/r")
    with pytest.raises(LedgerError):
        _cap(_forks(10), 99)  # this pull request is not in the list
    with pytest.raises(LedgerError):
        _cap([(42, "", "x/r")], 42, author="")  # an unknown author would match a list entry with no login


def test_a_hostile_login_is_not_echoed_in_the_pause_text() -> None:
    login = "@org/team [x](http://evil.example)"
    with pytest.raises(ForkBudgetPause) as stop:
        _cap([(n, login, "f/r") for n in (1, 2, 3)], 3, author=login)
    assert "evil.example" not in stop.value.reason and "@org" not in stop.value.reason


# --- the GitHub provider side ------------------------------------------------

_BOT = {"login": "github-actions[bot]"}
_USER = {"login": "mallory"}


def _provider(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubProvider:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    client = RecordingClient(
        http=http, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None))
    return GitHubProvider(config=GitHubConfig(owner="o", repo="r", pr_number=42, token="t"), client=client)


def _issue(number: int, *, user: dict[str, str] = _BOT, title: str = SPEND_ISSUE_TITLE,
           state: str = "open", pull: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {"number": number, "user": user, "title": title, "state": state}
    if pull:
        item["pull_request"] = {"url": "x"}
    return item


class Api:
    """A tiny GitHub: issue list, comments and the calls that were made."""

    def __init__(self, issues: list[dict[str, Any]], comments: list[dict[str, Any]] | None = None) -> None:
        self.issues, self.comments = issues, comments or []
        self.calls: list[tuple[str, str, str]] = []
        self.creator_empty = False
        self.search_items: list[dict[str, Any]] = []
        self.search_status = 200
        self.endless = False  # the issue listing never ends: every page links to a next page

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, query = req.url.path, req.url.query.decode()
        self.calls.append((req.method, path, query))
        if req.method == "GET" and path == "/search/issues":
            if self.search_status >= 400:
                return httpx.Response(self.search_status, json={})
            return httpx.Response(200, json={"items": self.search_items})
        if req.method == "GET" and path == "/repos/o/r/issues":
            if "creator=" not in query:
                raise AssertionError("the issue listing must always use the creator filter")
            if self.creator_empty:
                return httpx.Response(200, json=[])
            headers = {"link": '<https://api.github.com/repos/o/r/issues?creator=x&page=2>; rel="next"'} if self.endless else {}
            return httpx.Response(200, json=self.issues, headers=headers)
        if req.method == "POST" and path == "/repos/o/r/issues":
            self.issues.append(_issue(900))
            self.creator_empty = False  # the new issue shows up in the listing
            return httpx.Response(201, json={"number": 900})
        if req.method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=self.comments)
        if req.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 77})
        return httpx.Response(200, json={})

    def posts(self) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] == "POST"]


def test_look_alike_issues_are_ignored() -> None:
    api = Api([
        _issue(1, user=_USER),  # same title, wrong author
        _issue(2, title="ai-pr-review fork spend"),  # bot, wrong title
        _issue(3, title=SPEND_ISSUE_TITLE + " "),  # bot, near title
        _issue(4, pull=True),  # a pull request with the title
        _issue(5, user={}),  # no login
        _issue(6),  # the real one
    ])
    prov = _provider(api)
    assert prov._find_spend_issue() == 6
    assert api.posts() == []


def test_the_lowest_numbered_bot_issue_wins_and_a_closed_issue_is_used() -> None:
    api = Api([_issue(9), _issue(4, state="closed"), _issue(7)])
    prov = _provider(api)
    assert prov.create_spend_entry("b") == 77
    assert ("POST", "/repos/o/r/issues/4/comments", "") in api.calls
    assert not any(c[1] == "/repos/o/r/issues" and c[0] == "POST" for c in api.calls)  # nothing created


def test_no_spend_issue_creates_one_with_the_exact_title_then_comments_on_it() -> None:
    api = Api([_issue(1, user=_USER)])
    prov = _provider(api)
    assert prov.create_spend_entry("b") == 77
    created = [c for c in api.posts() if c[1] == "/repos/o/r/issues"]
    assert len(created) == 1
    assert ("POST", "/repos/o/r/issues/900/comments", "") in api.calls


def test_a_creator_filter_that_finds_nothing_falls_back_to_a_search_before_creating() -> None:
    api = Api([])
    api.creator_empty = True
    api.search_items = [_issue(3)]
    prov = _provider(api)
    prov.create_spend_entry("b")
    assert not any(c[1] == "/repos/o/r/issues" and c[0] == "POST" for c in api.calls)
    assert ("POST", "/repos/o/r/issues/3/comments", "") in api.calls
    search = next(c for c in api.calls if c[1] == "/search/issues")
    from urllib.parse import unquote_plus

    q = unquote_plus(search[2])
    assert "repo:o/r" in q and "is:issue" in q and "in:title" in q and "author:app/github-actions" in q
    assert SPEND_ISSUE_TITLE in q and "[bot]" not in q


def test_the_search_fallback_ignores_look_alikes_and_takes_the_lowest_number() -> None:
    api = Api([])
    api.creator_empty = True
    api.search_items = [_issue(8), _issue(2, user=_USER), _issue(5, pull=True), _issue(6), _issue(4, title="x")]
    assert _provider(api)._find_spend_issue() == 6
    assert api.posts() == []


def test_nothing_found_by_listing_or_search_creates_the_issue() -> None:
    api = Api([_issue(1, user=_USER)])
    api.creator_empty = True
    prov = _provider(api)
    prov.create_spend_entry("b")
    assert len([c for c in api.posts() if c[1] == "/repos/o/r/issues"]) == 1


def test_a_failed_search_fails_closed_and_creates_nothing() -> None:
    api = Api([])
    api.creator_empty = True
    api.search_status = 422
    prov = _provider(api)
    with pytest.raises(RuntimeError):
        prov.create_spend_entry("b")
    assert [c for c in api.posts() if c[1] == "/repos/o/r/issues"] == []


def test_a_huge_issue_listing_never_raises_and_never_scans_all_issues(caplog: pytest.LogCaptureFixture) -> None:
    api = Api([_issue(5)])
    api.endless = True
    with caplog.at_level("WARNING"):
        assert _provider(api)._find_spend_issue() == 5
    assert "more than" in caplog.text
    api2 = Api([])
    api2.endless = True
    api2.search_items = []
    assert _provider(api2)._find_spend_issue() is None  # a capped listing with no hit is not found, no raise


def test_reading_with_no_spend_issue_returns_nothing_and_creates_nothing() -> None:
    api = Api([])
    assert _provider(api).list_spend_entries("2026-10-20T00:00:00Z") == []
    assert api.posts() == []


def test_forged_spend_comments_from_a_non_bot_are_ignored() -> None:
    forged = render_spend_entry(TIER_SHARED, "1", 1, 99999, STATE_RESERVED, NOW)
    api = Api([_issue(3)], [
        {"id": 1, "user": _USER, "body": forged},
        {"id": 2, "user": _USER, "author_association": "OWNER", "body": forged},
        {"id": 4, "body": forged},
        {"id": 5, "user": _BOT, "body": "an unrelated bot comment"},
        {"id": 6, "user": _BOT, "body": render_spend_entry(TIER_SHARED, "1", 1, 40, STATE_RESERVED, NOW)},
    ])
    prov = _provider(api)
    assert [cid for cid, _ in prov.list_spend_entries("2026-10-20T00:00:00Z")] == [6]
    ForkSpend(prov, DailyCaps(), tier=TIER_SHARED, pr_number=42, clock=lambda: NOW).precheck()  # forged do not fill it


def test_the_listing_passes_since_and_pages_through_every_page() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/o/r/issues":
            return httpx.Response(200, json=[_issue(3)])
        seen.append(req.url.query.decode())
        if "page=2" in seen[-1]:
            return httpx.Response(200, json=[{"id": 2, "user": _BOT, "body": render_spend_entry(
                TIER_SHARED, "1", 1, 5, STATE_RESERVED, NOW)}])
        return httpx.Response(
            200, json=[{"id": 1, "user": _BOT, "body": render_spend_entry(TIER_SHARED, "1", 1, 5, STATE_RESERVED, NOW)}],
            headers={"link": '<https://api.github.com/repos/o/r/issues/3/comments?page=2>; rel="next"'})

    assert [c for c, _ in _provider(handler).list_spend_entries("2026-10-20T00:00:00Z")] == [1, 2]
    assert "since=2026-10-20T00%3A00%3A00Z" in seen[0]


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"not": "a list"}),
    httpx.Response(200, json=["not an object"]),
    httpx.Response(403, json={"message": "no"}),
])
def test_a_bad_listing_raises_in_every_call(response: httpx.Response) -> None:
    prov = _provider(lambda _req: response)
    with pytest.raises(RuntimeError):
        prov.list_open_pulls()
    with pytest.raises(RuntimeError):
        prov.list_spend_entries("2026-10-20T00:00:00Z")
    with pytest.raises(RuntimeError):
        prov.create_spend_entry("b")


def test_a_failed_page_raises_instead_of_returning_a_partial_list() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json=[{"number": 1, "user": _USER, "head": {"repo": None}}],
            headers={"link": '<https://api.github.com/repos/o/r/pulls?page=2>; rel="next"'})

    with pytest.raises(RuntimeError):
        _provider(handler).list_open_pulls()


def test_open_pulls_report_a_deleted_fork_as_an_empty_repository() -> None:
    prov = _provider(lambda _req: httpx.Response(200, json=[
        {"number": 3, "user": _USER, "head": {"repo": None}},
        {"number": 4, "user": _USER, "head": {"repo": {"full_name": "mallory/r"}}}]))
    assert prov.list_open_pulls() == [(3, "mallory", ""), (4, "mallory", "mallory/r")]


@pytest.mark.parametrize("status", [403, 410, 422, 500])
def test_a_refused_issue_or_comment_write_raises(status: int) -> None:
    api = Api([_issue(3)])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method in ("POST", "PATCH", "DELETE"):
            return httpx.Response(status, text="locked")
        return api(req)

    prov = _provider(handler)
    with pytest.raises(RuntimeError):
        prov.create_spend_entry("b")
    with pytest.raises(RuntimeError):
        prov.update_spend_entry(1, "b")
    with pytest.raises(RuntimeError):
        _provider(lambda r: httpx.Response(status) if r.method == "POST" else Api([])(r)).create_spend_entry("b")


# --- the config --------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(50.0, 50.0), (0.0, 0.0), (12.5, 12.5), (-1.0, 50.0), (float("nan"), 50.0),
                                               (float("inf"), 50.0)])
def test_the_daily_cap_falls_back_to_50_for_an_unusable_value(raw: float, expected: float) -> None:
    assert ReviewConfig(fork_daily_max_cost_usd=raw).fork_daily_max_cost_usd == expected


@pytest.mark.parametrize(("raw", "expected"), [(0.7, 0.7), (0.0, 0.0), (1.0, 1.0), (1.5, 0.7), (-0.1, 0.7),
                                               (float("nan"), 0.7)])
def test_the_reserved_share_falls_back_to_0_7_for_an_unusable_value(raw: float, expected: float) -> None:
    assert ReviewConfig(fork_daily_reserved_share=raw).fork_daily_reserved_share == expected


def test_the_defaults_are_50_usd_and_70_percent() -> None:
    cfg = ReviewConfig()
    assert (cfg.fork_daily_max_cost_usd, cfg.fork_daily_reserved_share) == (50.0, 0.7)


# --- settle in the CLI -------------------------------------------------------


def test_the_cli_settles_the_spend_entry_with_the_priced_actual_cost() -> None:
    mem = SpendMem()
    spend = _spend(mem)
    runtime = SimpleNamespace(
        fork_budget=None, fork_reservation=None, fork_spend=spend, fork_spend_reservation=spend.reserve(100))
    _settle_fork_budget(runtime, SimpleNamespace(cost_units=1234, any_unknown=False))
    assert mem.sum(TIER_SHARED) == 13
    assert "state=settled" in next(iter(mem.bot.values()))


def test_the_cli_keeps_the_spend_estimate_without_priced_totals_or_on_a_failed_edit() -> None:
    mem = SpendMem()
    spend = _spend(mem)
    runtime = SimpleNamespace(
        fork_budget=None, fork_reservation=None, fork_spend=spend, fork_spend_reservation=spend.reserve(100))
    _settle_fork_budget(runtime, SimpleNamespace(cost_units=1, any_unknown=True))
    assert mem.sum(TIER_SHARED) == 100
    mem.fail_update = True
    _settle_fork_budget(runtime, SimpleNamespace(cost_units=10, any_unknown=False))
    assert mem.sum(TIER_SHARED) == 100


# --- the runtime wiring ------------------------------------------------------


def _provider_class() -> type:
    from tests.python.test_fork_budget import Backend
    from tests.python.test_runtime import _FakeProvider

    class _Provider(_FakeProvider):
        """The fake provider of test_runtime plus the gate facts, the PR ledger and the spend ledger."""

        def __init__(self, mem: SpendMem, association: str = "NONE", same_repo: bool = False) -> None:
            super().__init__()
            self.mem, self.association, self.same_repo = mem, association, same_repo
            self.pr_ledger = Backend()
            self.calls: list[str] = []

        def get_fork_gate_facts(self, head_sha: str, context: str, *, read_approval: bool = True) -> ForkGateFacts:
            head = "o/r" if self.same_repo else "mallory/r"
            return ForkGateFacts(head, "o/r", "", "mallory", self.association)

        def list_budget_comments(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
            self.calls.append("pr")
            return self.pr_ledger.list_budget_comments()

        def create_budget_entry(self, body: str) -> int:
            return self.pr_ledger.create_budget_entry(body)

        def update_budget_entry(self, comment_id: int, body: str) -> None:
            self.pr_ledger.update_budget_entry(comment_id, body)

        def delete_budget_entry(self, comment_id: int) -> None:
            self.pr_ledger.delete_budget_entry(comment_id)

        def list_spend_entries(self, since: str) -> list[tuple[int, str]]:
            self.calls.append("spend")
            return self.mem.list_spend_entries(since)

        def create_spend_entry(self, body: str) -> int:
            return self.mem.create_spend_entry(body)

        def update_spend_entry(self, comment_id: int, body: str) -> None:
            self.mem.update_spend_entry(comment_id, body)

        def delete_spend_entry(self, comment_id: int) -> None:
            self.mem.delete_spend_entry(comment_id)

        def list_open_pulls(self) -> list[tuple[int, str, str]]:
            self.calls.append("pulls")
            return self.mem.list_open_pulls()

    return _Provider


@pytest.fixture(autouse=True)
def _pin_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the run pointer the same inside and outside ``_build``, whatever the runner sets."""
    monkeypatch.setenv("GITHUB_RUN_ID", "777")


async def _build(tmp_path: Path, provider: Any, **cfg: Any) -> Any:
    from ai_pr_review.review.runtime import build_review_runtime
    from tests.python.test_runtime import _make_config, _make_diff_result

    config = _make_config(event_name="pull_request_target", head_sha="a" * 40, fork_policy="auto", **cfg)
    with (
        patch("ai_pr_review.review.fork_gate.workspace_head", return_value="a" * 40),
        patch("ai_pr_review.diff.compute.compute_diff", return_value=_make_diff_result()) as compute,
        patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
        patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(tmp_path / "diff.txt"), "GITHUB_RUN_ID": "777"},
                   clear=False),
    ):
        result = await build_review_runtime(config, provider_factory=lambda: provider)
    return result, compute


@pytest.mark.anyio
async def test_a_fork_run_reserves_in_both_ledgers_before_any_llm_call(tmp_path: Path) -> None:
    mem = SpendMem()
    runtime, _ = await _build(tmp_path, _provider_class()(mem))
    assert runtime.fork_reservation is not None and runtime.fork_spend_reservation is not None
    entry = parse_spend_entry(1, mem.bot[runtime.fork_spend_reservation.comment_id])
    assert (entry.tier, entry.pr, entry.state) == (TIER_SHARED, 42, STATE_RESERVED)
    assert "run=777" in mem.bot[runtime.fork_spend_reservation.comment_id]
    assert entry.cents == runtime.fork_spend_reservation.estimate_cents == runtime.fork_reservation.estimate_cents > 0


@pytest.mark.anyio
async def test_the_third_fork_pr_of_an_author_pauses_before_compute_and_makes_no_entry(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    mem = SpendMem()
    mem.pulls = [(5, "mallory", "m/r"), (6, "mallory", "m/r"), (42, "mallory", "m/r")]
    provider = _provider_class()(mem)
    result, compute = await _build(tmp_path, provider)
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and "2 open fork pull requests" in result.reason
    compute.assert_not_called()
    assert mem.bot == {} and provider.pr_ledger.bot == {} and "spend" not in provider.calls


@pytest.mark.anyio
async def test_a_full_shared_pool_pauses_a_none_author_but_not_a_contributor(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import ReviewRuntime, SkipPlan

    mem = SpendMem()
    mem.add(1500, TIER_SHARED, now=datetime.now(UTC))
    provider = _provider_class()(mem, association="NONE")
    result, compute = await _build(tmp_path, provider)
    assert isinstance(result, SkipPlan) and "daily" in result.reason and "24 hour window" in result.reason
    compute.assert_not_called()
    assert len(mem.bot) == 1 and provider.pr_ledger.bot == {}  # a paused run appends nothing
    runtime, _ = await _build(tmp_path, _provider_class()(mem, association="CONTRIBUTOR"))
    assert isinstance(runtime, ReviewRuntime) and runtime.fork_spend_reservation is not None
    assert parse_spend_entry(1, mem.bot[runtime.fork_spend_reservation.comment_id]).tier == TIER_RESERVED


@pytest.mark.anyio
async def test_an_estimate_that_does_not_fit_the_daily_pool_pauses_and_appends_nothing(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    mem = SpendMem()
    mem.add(1499, TIER_SHARED, now=datetime.now(UTC))  # the precheck passes, 1 cent is left
    provider = _provider_class()(mem)
    result, _ = await _build(tmp_path, provider)
    assert isinstance(result, SkipPlan) and "daily" in result.reason
    assert len(mem.bot) == 1 and provider.pr_ledger.bot == {}


@pytest.mark.anyio
@pytest.mark.parametrize("fail", ["fail_read", "fail_create", "fail_pulls"])
async def test_a_spend_or_author_read_or_write_failure_pauses_the_fork_run(tmp_path: Path, fail: str) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    mem = SpendMem()
    setattr(mem, fail, True)
    result, _ = await _build(tmp_path, _provider_class()(mem))
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and result.reason == with_run_pointer(LEDGER_ERROR_TEXT)
    assert mem.bot == {}


@pytest.mark.anyio
async def test_a_provider_without_a_spend_ledger_pauses_the_fork_run(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    class _NoSpend(_provider_class()):  # type: ignore[misc]
        list_spend_entries = None  # type: ignore[assignment]

    result, _ = await _build(tmp_path, _NoSpend(SpendMem()))
    assert isinstance(result, SkipPlan) and result.reason == with_run_pointer(LEDGER_ERROR_TEXT)


@pytest.mark.anyio
async def test_a_pr_ledger_failure_after_the_daily_reserve_releases_the_daily_entry(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    mem = SpendMem()
    provider = _provider_class()(mem)
    provider.pr_ledger.fail_create = True  # the PR reserve fails after the daily reserve passed
    result, _ = await _build(tmp_path, provider)
    assert isinstance(result, SkipPlan) and result.reason == with_run_pointer(LEDGER_ERROR_TEXT)
    assert mem.bot == {}


@pytest.mark.anyio
async def test_trusted_non_fork_and_dry_runs_make_no_spend_issue_or_pull_list_call(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import ReviewRuntime

    for kwargs, cfg in (
        ({"association": "OWNER"}, {}),
        ({"association": "COLLABORATOR"}, {}),
        ({"same_repo": True}, {}),
        ({}, {"dry_run": True}),
    ):
        mem = SpendMem()
        mem.fail_read = mem.fail_create = mem.fail_pulls = True  # any call would pause the run
        provider = _provider_class()(mem, **kwargs)
        runtime, _ = await _build(tmp_path, provider, **cfg)
        assert isinstance(runtime, ReviewRuntime), (kwargs, cfg)
        assert runtime.fork_spend is None and runtime.fork_spend_reservation is None
        assert "spend" not in provider.calls and "pulls" not in provider.calls
        assert mem.since == [] and mem.creates == 0


@pytest.mark.anyio
async def test_the_rescan_credit_does_not_change_the_author_or_the_daily_cap(tmp_path: Path) -> None:
    from ai_pr_review.review.runtime import SkipPlan

    mem = SpendMem()
    mem.add(1500, TIER_SHARED, now=datetime.now(UTC))
    provider = _provider_class()(mem)
    provider.pr_ledger.maintainer = [(900, "/ai-pr-review rescan")]
    result, _ = await _build(tmp_path, provider)
    assert isinstance(result, SkipPlan) and "daily" in result.reason


def test_a_failed_reread_after_the_write_releases_the_own_entry() -> None:
    mem = SpendMem()
    mem.after_create = lambda: setattr(mem, "fail_read", True)
    with pytest.raises(LedgerError):
        _spend(mem).reserve(10)
    mem.fail_read = False
    assert mem.bot == {}


def test_a_failed_reread_and_a_failed_release_stay_charged_and_pause() -> None:
    mem = SpendMem()

    def flap() -> None:
        mem.fail_read = True
        mem.fail_delete = True

    mem.after_create = flap
    with pytest.raises(LedgerError):
        _spend(mem).reserve(10)
    mem.fail_read = mem.fail_delete = False
    assert len(mem.bot) == 1


def test_a_malformed_spend_comment_error_names_the_comment_id_and_not_the_body() -> None:
    mem = SpendMem()
    mem.bot[777] = "<!-- ai-pr-review-fork-spend tier=bogus INJECTED-FORK-TEXT -->"
    with pytest.raises(LedgerError) as caught:
        _spend(mem).precheck()
    assert "777" in str(caught.value)
    assert "INJECTED-FORK-TEXT" not in str(caught.value)
