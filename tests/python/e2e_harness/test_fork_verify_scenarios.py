"""Unit tests for tests/e2e/fork_verify.py: S9 to S17. No network calls."""

from __future__ import annotations

from ai_pr_review.review.fork_budget import LEDGER_ERROR_TEXT, Caps, Entry, pause_text
from ai_pr_review.review.fork_spend import DailyCaps, author_pause_text, daily_pause_text
from tests.e2e import fork_verify as fv
from tests.python.e2e_harness.fork_builders import (
    AUTHOR,
    HEAD,
    MAINTAINER,
    comment,
    evidence,
    inline,
    ledger,
    review,
    s3_evidence,
    skip,
    spend,
    spend_issue,
    summary,
)

RESCAN = "/ai-pr-review rescan"


def _runs_text(n: int = 4) -> str:
    return pause_text("runs", [Entry(i, 1, 10, 0) for i in range(n)], Caps())


def _capped(n: int = 4) -> list:
    return [ledger(i + 2, cents=10) for i in range(n)]


# --- S9 --------------------------------------------------------------------------


def test_s9_pass_and_fail():
    own = comment(8, AUTHOR, RESCAN)
    ok = evidence(issue_comments=[*_capped(), own, skip(9, _runs_text())])
    assert fv.verify_s9(ok).ok
    credited = evidence(issue_comments=[*_capped(), own, ledger(7, 0, 0, 1, 8), skip(9, _runs_text())])
    assert "credit" in fv.verify_s9(credited).reason
    assert not fv.verify_s9(evidence(issue_comments=[*_capped(), skip(9, _runs_text())])).ok  # no rescan comment
    assert not fv.verify_s9(evidence(issue_comments=[*_capped(), own])).ok  # no pause


# --- S10 -------------------------------------------------------------------------


def test_s10_pass_and_fail():
    cm = comment(8, MAINTAINER, RESCAN)
    base = [*_capped(), cm, ledger(9, 0, 0, 1, 8), ledger(10, cents=30)]
    ok = evidence(issue_comments=[*base, summary(11)], reviews=[review()])
    assert fv.verify_s10(ok, maintainer_login=MAINTAINER, expected_runs=5).ok
    wrong_runs = fv.verify_s10(ok, maintainer_login=MAINTAINER, expected_runs=6)
    assert "runs" in wrong_runs.reason
    assert not fv.verify_s10(ok, maintainer_login="other", expected_runs=5).ok
    two = evidence(issue_comments=[*base, ledger(12, 0, 0, 1, 99)], reviews=[review()])
    assert "one credit" in fv.verify_s10(two, maintainer_login=MAINTAINER, expected_runs=5).reason
    no_review = evidence(issue_comments=base)
    assert "no review" in fv.verify_s10(no_review, maintainer_login=MAINTAINER, expected_runs=5).reason
    post_credit = pause_text("runs", [*[Entry(i, 1, 10, 0) for i in range(5)], Entry(9, 0, 0, 0, 8)], Caps())
    rebind = evidence(issue_comments=[*base, skip(20, post_credit)], reviews=[review()])
    assert fv.verify_s10(rebind, maintainer_login=MAINTAINER, expected_runs=5, expect_rebind=True).ok
    assert not fv.verify_s10(ok, maintainer_login=MAINTAINER, expected_runs=5, expect_rebind=True).ok
    # The pause comment of S7 stays on the pull request and must not count as a new pause.
    stale = evidence(issue_comments=[*base, skip(20, _runs_text())], reviews=[review()])
    assert "did not bind" in fv.verify_s10(stale, maintainer_login=MAINTAINER, expected_runs=5, expect_rebind=True).reason
    forged = "c" * 40
    taken = evidence(issue_comments=[*base, comment(23, fv.BOT_LOGIN, "x\n<!-- ai-pr-review-summary sha=" + forged + " -->")],
                     reviews=[review()])
    assert "forged watermark" in fv.verify_s10(taken, maintainer_login=MAINTAINER, expected_runs=5, forged_sha=forged).reason
    assert forged != HEAD


# --- S11 ---------------------------------------------------------------------------


def test_s11_pass_and_fail():
    text = daily_pause_text("shared", 1500, DailyCaps())
    assert fv.verify_s11(evidence(issue_comments=[skip(1, text)]), seeded_cents=1500).ok
    assert not fv.verify_s11(evidence(issue_comments=[skip(1, text)]), seeded_cents=1400).ok
    added = evidence(issue_comments=[skip(1, text)], spend_comments=[spend(5)])
    assert "spend entry" in fv.verify_s11(added, seeded_cents=1500).reason
    assert not fv.verify_s11(evidence(), seeded_cents=1500).ok


# --- S12 ---------------------------------------------------------------------------


def _pr(n: int, *, reviewed: bool, paused: bool = False) -> fv.ForkEvidence:
    kw: dict = {"pr_number": n}
    if reviewed:
        kw.update(issue_comments=[summary(n * 10)], reviews=[review(n * 10 + 1)], review_comments=[inline(n * 10 + 2)])
    if paused:
        kw.update(issue_comments=[skip(n * 10, author_pause_text(AUTHOR, 3))])
    return evidence(**kw)


def test_s12_pass_and_fail():
    ok = [_pr(1, reviewed=True), _pr(2, reviewed=True), _pr(3, reviewed=False, paused=True)]
    assert fv.verify_s12(ok).ok
    assert not fv.verify_s12([_pr(1, reviewed=True), _pr(2, reviewed=False), ok[2]]).ok
    reviewed_third = [ok[0], ok[1], _pr(3, reviewed=True)]
    assert not fv.verify_s12(reviewed_third).ok
    assert fv.verify_s12(ok[:2]).category == "infra_failure"
    ledgered = evidence(pr_number=3, issue_comments=[skip(30, author_pause_text(AUTHOR, 3)), ledger(31)])
    assert not fv.verify_s12([ok[0], ok[1], ledgered]).ok


# --- S13 ---------------------------------------------------------------------------


def test_s13_records_either_answer():
    locked = spend_issue(locked=True)
    can = s3_evidence(spend_issues=[locked])
    v = fv.verify_s13(can)
    assert v.ok and "CAN comment" in v.reason
    paused = evidence(issue_comments=[skip(1, LEDGER_ERROR_TEXT)], spend_issues=[locked])
    v2 = fv.verify_s13(paused)
    assert v2.ok and "CANNOT" in v2.reason
    assert fv.verify_s13(evidence(spend_issues=[locked])).ok is False  # neither a review nor a pause
    assert fv.verify_s13(s3_evidence()).category == "infra_failure"  # not locked
    no_spend = s3_evidence(spend_issues=[locked], spend_comments=[])
    assert not fv.verify_s13(no_spend).ok


# --- S14 ---------------------------------------------------------------------------


def test_s14_pass_and_fail():
    two = [ledger(2, cents=40), ledger(3, cents=45)]
    spends = [spend(100, run="111", state="reserved"), spend(101, run="222", state="settled")]
    runs = [{"id": 111, "conclusion": "cancelled"}, {"id": 222, "conclusion": "success"}]
    kw = {"cancelled_run_id": "111", "completed_run_id": "222"}
    assert fv.verify_s14(evidence(issue_comments=two, spend_comments=spends, runs=runs), **kw).ok
    early = fv.verify_s14(evidence(issue_comments=two[1:], spend_comments=spends[1:]), **kw)
    assert early.category == "infra_failure"
    settled_both = [spend(100, run="111", state="settled"), spends[1]]
    assert not fv.verify_s14(evidence(issue_comments=two, spend_comments=settled_both), **kw).ok
    not_cancelled = [{"id": 111, "conclusion": "success"}]
    assert not fv.verify_s14(evidence(issue_comments=two, spend_comments=spends, runs=not_cancelled), **kw).ok
    assert not fv.verify_s14(evidence(issue_comments=two[:1], spend_comments=spends), **kw).ok


# --- S15 ---------------------------------------------------------------------------


def _forged() -> list:
    return [
        comment(20, AUTHOR, ledger(0, 0, 0, 1, 19)["body"]),
        comment(21, AUTHOR, ledger(0, 1, 0)["body"]),
    ]


def test_s15_pass_and_fail():
    kw = {"expected_bot_runs": 4, "expected_bot_cents": 40}
    base = [*_capped(), *_forged(), skip(30, _runs_text())]
    assert fv.verify_s15(evidence(issue_comments=base), **kw).ok
    no_forgery = fv.verify_s15(evidence(issue_comments=[*_capped(), skip(30, _runs_text())]), **kw)
    assert no_forgery.category == "infra_failure"
    reviewed = evidence(issue_comments=base, reviews=[review()])
    assert not fv.verify_s15(reviewed, **kw).ok
    lifted = evidence(issue_comments=[*_capped(), ledger(25, 0, 0, 1, 19), *_forged(), skip(30, _runs_text())])
    assert "allowance" in fv.verify_s15(lifted, **kw).reason


# --- S16 ---------------------------------------------------------------------------


def test_s16_pass_and_fail():
    assert fv.verify_s16(evidence(spend_issues=[spend_issue()], spend_comments=[spend(1)]), min_entries=1).ok
    assert "found 2" in fv.verify_s16(evidence(spend_issues=[spend_issue(7), spend_issue(8)])).reason
    assert "found 0" in fv.verify_s16(evidence()).reason
    assert "not created by the bot" in fv.verify_s16(evidence(spend_issues=[spend_issue(login="gchaix")])).reason
    assert "closed" in fv.verify_s16(evidence(spend_issues=[spend_issue(state="closed")])).reason
    lookalike = spend_issue(pull_request={})
    assert not fv.verify_s16(evidence(spend_issues=[lookalike])).ok
    assert not fv.verify_s16(evidence(spend_issues=[spend_issue()]), min_entries=1).ok


# --- S17 ---------------------------------------------------------------------------


def test_s17_pass_and_fail():
    tagged = s3_evidence(review_comments=[inline()])
    assert fv.verify_s17(tagged).ok
    assert "[phpcs]" in fv.verify_s17(s3_evidence(review_comments=[inline(text="[ruff] only")])).reason
    fired = fv.verify_s17(s3_evidence(review_comments=[inline()], canary_listing=("E2E-PWNED-eslint",)))
    assert "canary file present" in fired.reason
    wrong = summary(skipped=["phpcs", "semgrep"])
    assert "which a fork run does run" in fv.verify_s17(s3_evidence(review_comments=[inline()], issue_comments=[wrong, ledger(5)])).reason


def test_verifier_table_covers_s1_to_s17():
    assert sorted(fv.VERIFIERS, key=lambda k: int(k[1:])) == [f"S{n}" for n in range(1, 18)]
