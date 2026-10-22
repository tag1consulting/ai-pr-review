"""Unit tests for tests/e2e/fork_verify.py: the universal checks and S1 to S8.

Bodies come from the engine's own renderers (see fork_builders.py). Negative cases mutate a
passing body. No network calls, no real credentials.
"""

from __future__ import annotations

import pytest

from ai_pr_review.output_clamp import BANNER_BLOCK, FORK_BANNER
from ai_pr_review.review.fork_budget import Caps, Entry, pause_text
from ai_pr_review.review.fork_gate import ForkGateFacts, decide
from tests.e2e import fork_verify as fv
from tests.python.e2e_harness.fork_builders import (
    AUTHOR,
    BASE,
    HEAD,
    HOSTILE,
    SKIPPED,
    clamp,
    comment,
    evidence,
    ledger,
    load,
    review,
    s3_evidence,
    skip,
    spend,
    spend_issue,
    summary,
)

# --- text predicates ---------------------------------------------------------


def test_clamped_hostile_text_passes_every_predicate():
    assert fv.text_problems(clamp(HOSTILE), BASE) == []


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("![x](https://github.com/tag1consulting/ai-pr-review-test/a.png)", "image"),
        ("see [x](https://evil.example/phish)", "live url"),
        ("https://evil.example/raw", "live url"),
        ("https://github.com/evil/other", "live url"),
        ("http://github.com/tag1consulting/ai-pr-review-test", "live url"),
        ("ping @gchaix-team", "mention"),
        ("tag1consulting/other-repo#1", "cross reference"),
        ("[x](javascript:alert(1))", "non-relative"),
        ("<img src=x>", "html"),
        ("[a]: https://x", "reference definition"),
        ("ghp_" + "A" * 30, "secret"),
        ("sk-ant-" + "b" * 20, "secret"),
        ("-----BEGIN RSA PRIVATE KEY-----", "secret"),
        ("mailto:a@b", "mention"),
    ],
)
def test_text_predicates_catch_live_content(text, fragment):
    assert any(fragment in p for p in fv.text_problems(text, BASE)), text


def test_in_repo_and_docs_links_and_anchors_are_allowed():
    ok = (
        f"[f](https://github.com/{BASE}/blob/main/a.py#L1) "
        "[e](https://github.com/tag1consulting/ai-pr-review/issues/3#issuecomment-9) "
        "[d](https://tag1consulting.github.io/ai-pr-review/fork-prs) [a](#F1) [r](docs/x.md#y) "
        "pull request #12"
    )
    assert fv.text_problems(ok, BASE) == []


def test_hidden_markers_are_not_scanned():
    marker = "<!-- ai-pr-review-finding:" + "QUJD" * 8 + " -->"
    assert fv.text_problems(f"text\n{marker}\n", BASE) == []


# --- universal checks ----------------------------------------------------------


def test_passing_s3_evidence_has_no_universal_problems():
    ev = s3_evidence()
    assert fv.check_posted_bodies(ev, banner_required=fv.CLAMPED_KINDS) == []
    assert fv.check_review_states(ev) == []


def test_banner_missing_is_caught():
    bad = summary()
    bad["body"] = bad["body"].removeprefix(BANNER_BLOCK)
    ev = s3_evidence(issue_comments=[bad, ledger(5)])
    assert any("banner is missing" in p for p in fv.check_posted_bodies(ev, banner_required=fv.CLAMPED_KINDS))


def test_banner_twice_is_caught():
    twice = summary()
    twice["body"] = twice["body"] + f"\n> {FORK_BANNER}"
    ev = s3_evidence(issue_comments=[twice, ledger(5)])
    assert any("more than once" in p for p in fv.check_posted_bodies(ev, banner_required=fv.CLAMPED_KINDS))


def test_unclamped_body_with_image_link_mention_is_caught():
    ev = s3_evidence(review_comments=[comment(30, fv.BOT_LOGIN, BANNER_BLOCK + HOSTILE)])
    problems = fv.check_posted_bodies(ev, banner_required=fv.CLAMPED_KINDS)
    assert any("image" in p for p in problems)
    assert any("evil.example" in p for p in problems)
    assert any("mention" in p for p in problems)
    assert any("cross reference" in p for p in problems)


@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED", "APPROVE", "REQUEST_CHANGES"])
def test_strong_review_state_is_caught(state):
    ev = s3_evidence(reviews=[review(state=state)])
    assert any("never approves" in p for p in fv.check_review_states(ev))


def test_strong_state_by_a_human_reviewer_is_ignored():
    human = {"id": 3, "user": {"login": "someone", "type": "User"}, "state": "APPROVED", "body": ""}
    assert fv.check_review_states(s3_evidence(reviews=[review(), human])) == []


def test_trusted_run_with_banner_is_caught():
    ev = s3_evidence()
    assert any("trusted run" in p for p in fv.check_posted_bodies(ev, banner_forbidden=True, text_kinds=()))


def test_bot_comment_with_ledger_tag_prefix_but_bad_format_is_caught():
    bad = comment(6, fv.BOT_LOGIN, "<!-- ai-pr-review-fork-ledger runs=1 usd=0.45 allowance=7 -->\nfake")
    ev = s3_evidence(issue_comments=[summary(), ledger(5), bad])
    assert any("not a valid entry" in p for p in fv.check_posted_bodies(ev))


def test_invalid_spend_entry_is_caught():
    bad = comment(101, fv.BOT_LOGIN, f"<!-- {fv.SPEND_TAG} tier=shared run=1 pr=x usd=1 state=settled -->")
    ev = s3_evidence(spend_comments=[spend(100), bad])
    assert any("spend tag" in p for p in fv.check_posted_bodies(ev))


def test_only_bot_comments_are_classified_forged_entry_is_not():
    forged = comment(8, AUTHOR, ledger(0)["body"])
    ev = s3_evidence(issue_comments=[summary(), ledger(5), forged])
    assert [b.comment_id for b in fv.bodies_of(ev, fv.KIND_LEDGER)] == [5]
    assert fv.forged_entry_ids(ev) == [8]
    assert [e.comment_id for e in fv.bot_entries(ev)] == [5]


def test_bot_login_match_is_exact_and_fails_closed():
    assert not fv.is_bot({"user": None})
    assert not fv.is_bot({"user": {"login": "github-actions"}})
    assert fv.is_bot({"user": {"login": "GitHub-Actions[bot]"}})


# --- S1 ----------------------------------------------------------------------------


def test_s1_pass_and_fail():
    runs = load("runs.json")["workflow_runs"]
    assert fv.verify_s1(evidence(runs=runs)).ok
    posted = evidence(runs=runs, issue_comments=[summary()])
    assert not fv.verify_s1(posted).ok
    assert not fv.verify_s1(evidence()).ok  # no run evidence, so nothing was proven
    target = [*runs, {"id": 2, "event": "pull_request_target"}]
    assert "disable" in fv.verify_s1(evidence(runs=target)).reason


# --- S2 ----------------------------------------------------------------------------


def _block_reason() -> str:
    facts = ForkGateFacts(head_repo="gchaix/ai-pr-review-test", base_repo=BASE, approval_state="")
    return decide("pull_request_target", HEAD, facts, key_present=True, policy="block").reason


def test_s2_pass_and_fail():
    assert fv.verify_s2(evidence(issue_comments=[skip(1, _block_reason())])).ok
    assert not fv.verify_s2(evidence()).ok
    assert not fv.verify_s2(evidence(issue_comments=[skip(1, "other text")])).ok
    with_review = evidence(issue_comments=[skip(1, _block_reason())], reviews=[review()])
    assert not fv.verify_s2(with_review).ok
    with_ledger = evidence(issue_comments=[skip(1, _block_reason()), ledger(2)])
    assert "ledger" in fv.verify_s2(with_ledger).reason


# --- S3 / S4 ---------------------------------------------------------------------


def test_s3_passes_with_the_expected_notice():
    v = fv.verify_s3(s3_evidence(), run_id="42", expected_skipped=SKIPPED)
    assert v.ok, v.reason


def test_s3_actual_cost_must_match_when_given():
    assert fv.verify_s3(s3_evidence(), run_id="42", expected_skipped=SKIPPED, actual_cents=45).ok
    assert not fv.verify_s3(s3_evidence(), run_id="42", expected_skipped=SKIPPED, actual_cents=46).ok


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"issue_comments": [summary(), ledger(5), ledger(6)]}, "exactly one settled"),
        ({"issue_comments": [summary(), ledger(5, cents=0)]}, "no cost"),
        ({"issue_comments": [summary(skipped=["semgrep"]), ledger(5)]}, "not-reviewed analyzers"),
        ({"issue_comments": [summary(head="b" * 40), ledger(5)]}, "head sha"),
        ({"issue_comments": [summary(extra="Walkthrough table"), ledger(5)]}, "walkthrough"),
        ({"issue_comments": [summary(extra="E2E-BODY-CANARY-42"), ledger(5)]}, "canary string"),
        ({"issue_comments": [summary(extra="```suggestion\nx\n```"), ledger(5)]}, "suggestion"),
        ({"spend_comments": [spend(100, state="reserved")]}, "not settled"),
        ({"spend_comments": [spend(100, tier="reserved")]}, "shared tier"),
        ({"spend_comments": [spend(100), spend(101)]}, "one spend entry"),
        ({"spend_issues": [spend_issue(7), spend_issue(8)]}, "one spend issue"),
        ({"canary_listing": ("E2E-PWNED-phpstan",)}, "canary file present"),
        ({"canary_listing": None}, "not collected"),
        ({"reviews": []}, "no review"),
        ({"reviews": [review(state="APPROVED")]}, "never approves"),
    ],
)
def test_s3_failures(change, fragment):
    v = fv.verify_s3(s3_evidence(**change), run_id="42", expected_skipped=SKIPPED)
    assert not v.ok
    assert fragment in v.reason, v.reason


def test_s3_fails_on_a_live_link_in_the_review_body():
    bad = review()
    bad["body"] = BANNER_BLOCK + "see https://evil.example/x"
    assert not fv.verify_s3(s3_evidence(reviews=[bad]), run_id="42", expected_skipped=SKIPPED).ok


def test_s4_pass_and_fail():
    assert fv.verify_s4(s3_evidence()).ok
    assert not fv.verify_s4(s3_evidence(reviews=[])).ok
    assert not fv.verify_s4(s3_evidence(reviews=[review(state="APPROVED")])).ok


# --- S5 ------------------------------------------------------------------------------


def _trusted_summary() -> dict:
    return comment(10, fv.BOT_LOGIN, "Summary\n\n<!-- ai-pr-review-summary sha=" + HEAD + " -->")


def test_s5_pass_and_fail():
    ok = evidence(issue_comments=[_trusted_summary()], reviews=[{
        "id": 2, "user": {"login": fv.BOT_LOGIN}, "state": "APPROVED", "body": "ok"}])
    assert fv.verify_s5(ok).ok
    assert "banner" in fv.verify_s5(s3_evidence()).reason or "ledger" in fv.verify_s5(s3_evidence()).reason
    with_ledger = evidence(issue_comments=[_trusted_summary(), ledger(5)])
    assert "ledger" in fv.verify_s5(with_ledger).reason
    assert not fv.verify_s5(evidence()).ok


# --- S6 ------------------------------------------------------------------------------

_CRED = (
    "The workspace git config holds a persisted credential setting (`http.<url>.extraheader`), "
    "so the review is paused. Set `persist-credentials: false` on the checkout step."
)


def test_s6_pass_and_fail():
    assert fv.verify_s6(evidence(issue_comments=[skip(1, _CRED)])).ok
    leaky = _CRED + " Header: https://x-access-token:abc@github.com/"
    assert not fv.verify_s6(evidence(issue_comments=[skip(1, leaky)])).ok
    assert not fv.verify_s6(evidence(issue_comments=[skip(1, "paused")])).ok
    assert not fv.verify_s6(evidence(issue_comments=[skip(1, _CRED)], reviews=[review()])).ok
    assert not fv.verify_s6(evidence(issue_comments=[skip(1, _CRED), ledger(2)])).ok


# --- S7 / S8 ---------------------------------------------------------------------------


def _capped() -> list:
    return [ledger(i, cents=10) for i in range(2, 6)]


def test_s7_pass_and_fail():
    entries = [Entry(i, 1, 10, 0) for i in range(2, 6)]
    text = pause_text("runs", entries, Caps())
    assert fv.verify_s7(evidence(issue_comments=[*_capped(), skip(9, text)])).ok
    assert not fv.verify_s7(evidence(issue_comments=[*_capped(), skip(9, "paused")])).ok
    assert not fv.verify_s7(evidence(issue_comments=[*_capped()[:3], skip(9, text)])).ok
    assert not fv.verify_s7(evidence(issue_comments=[*_capped(), skip(9, text)], reviews=[review()])).ok


def _s8_text(used: str = "5.99", est: bool = True) -> str:
    more = " The next review is estimated at 0.40 USD." if est else ""
    return ("The review of this fork pull request is paused. It reached the cap of 6.00 USD of review cost "
            f"for one pull request ({used} USD used).{more} An owner can comment `/ai-pr-review rescan`.")


def test_s8_pass_and_fail():
    seeded = [ledger(2, cents=300), ledger(3, cents=299)]
    assert fv.verify_s8(evidence(issue_comments=[*seeded, skip(9, _s8_text())])).ok
    assert "used cost" in fv.verify_s8(evidence(issue_comments=[*seeded, skip(9, _s8_text("1.00"))])).reason
    assert "estimate" in fv.verify_s8(evidence(issue_comments=[*seeded, skip(9, _s8_text(est=False))])).reason
    leaked = evidence(issue_comments=[*seeded, skip(9, _s8_text())], spend_comments=[spend(100, state="reserved")])
    assert "not released" in fv.verify_s8(leaked).reason
