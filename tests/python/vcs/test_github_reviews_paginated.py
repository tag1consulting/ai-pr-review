"""GitHubProvider._list_reviews_paginated — the shared walk over
GET /pulls/{n}/reviews that both list_bot_reviews() and
_list_prior_bot_reviews() now go through (consolidation follow-up to
PR #716's comprehensive review: those two methods used to be independent,
hand-written pagination loops with drifted error-handling and
state-filtering contracts, risking `select_canonical()` picking a different
canonical review on the write side than the read side purely from a
pagination-bug divergence rather than a deliberate difference).

This file locks in:
- the shared walk's own strict/states parameterization,
- that list_bot_reviews() and _list_prior_bot_reviews() each still produce
  their pre-existing, independently-tested external behavior through it, and
- that given the SAME underlying review list, the two walks' outputs lead
  `select_canonical()` to the same canonical review -- proving the write
  side (list_bot_reviews(), feeding _record_verdict) and the read side
  (_list_prior_bot_reviews(), feeding post_findings's classification) can no
  longer structurally disagree about the review-listing mechanics itself.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.vcs._canonical import select_canonical
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GitHubProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.github.com")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    return GitHubProvider(config=config, client=client)


def _review(rid: int, state: str, *, login: str = "github-actions[bot]", body: str = "") -> dict:
    return {"id": rid, "state": state, "user": {"login": login}, "body": body}


# ---------------------------------------------------------------------------
# strict=True: [] on any HTTP error mid-pagination, never touches self._errors
# ---------------------------------------------------------------------------


def test_strict_true_returns_empty_list_on_mid_pagination_error() -> None:
    """Page 1 succeeds; page 2 (reached via Link: rel=next) fails. strict=True
    must discard the already-collected page 1 results entirely (the
    #550/#553 guarantee `_list_prior_bot_reviews()` depends on), not return
    them as partial results."""
    page1 = [_review(1, "COMMENTED")]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "page=2" in url:
            return httpx.Response(500, text="boom")
        base = "https://api.github.com/repos/o/r/pulls/1/reviews"
        link = f'<{base}?page=2>; rel="next"'
        return httpx.Response(200, json=page1, headers={"link": link})

    prov = _make_provider(handler)
    result = prov._list_reviews_paginated(strict=True)

    assert result == []
    assert prov._errors == []


def test_strict_false_returns_partial_results_and_records_error() -> None:
    """Same two-page failure shape, but strict=False must keep page 1's
    results and append the failure to self._errors instead of discarding
    everything -- the contract every list_bot_reviews() caller (e.g.
    ai_pr_review.slash.dismiss's classification/verdict-recording call
    sites) already relies on."""
    page1 = [_review(1, "COMMENTED")]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "page=2" in url:
            return httpx.Response(500, text="boom")
        base = "https://api.github.com/repos/o/r/pulls/1/reviews"
        link = f'<{base}?page=2>; rel="next"'
        return httpx.Response(200, json=page1, headers={"link": link})

    prov = _make_provider(handler)
    result = prov._list_reviews_paginated(strict=False)

    assert result == page1
    assert len(prov._errors) == 1
    assert "500" in prov._errors[0]


def test_states_filter_excludes_non_matching_states() -> None:
    reviews = [
        _review(1, "PENDING"),
        _review(2, "COMMENTED"),
        _review(3, "APPROVED"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reviews)

    prov = _make_provider(handler)
    result = prov._list_reviews_paginated(
        strict=True, states=frozenset({"COMMENTED", "APPROVED"})
    )

    assert {r["id"] for r in result} == {2, 3}


def test_states_none_keeps_every_state_including_pending() -> None:
    reviews = [_review(1, "PENDING"), _review(2, "COMMENTED")]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reviews)

    prov = _make_provider(handler)
    result = prov._list_reviews_paginated(strict=False, states=None)

    assert {r["id"] for r in result} == {1, 2}


def test_bot_login_filter_applies_regardless_of_strict_or_states() -> None:
    reviews = [
        _review(1, "COMMENTED", login="github-actions[bot]"),
        _review(2, "COMMENTED", login="some-human"),
        _review(3, "COMMENTED", login="dependabot[bot]"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reviews)

    prov = _make_provider(handler)

    strict_result = prov._list_reviews_paginated(strict=True)
    lenient_result = prov._list_reviews_paginated(strict=False)

    assert [r["id"] for r in strict_result] == [1]
    assert [r["id"] for r in lenient_result] == [1]


# ---------------------------------------------------------------------------
# list_bot_reviews() / _list_prior_bot_reviews() still express their
# pre-existing, independently-documented contracts through the shared walk.
# ---------------------------------------------------------------------------


def test_list_bot_reviews_keeps_pending_and_returns_full_review_dicts() -> None:
    reviews = [_review(1, "PENDING", body="draft"), _review(2, "APPROVED", body="done")]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reviews)

    prov = _make_provider(handler)
    result = prov.list_bot_reviews()

    assert {r["id"] for r in result} == {1, 2}
    # Full review dict, not narrowed -- callers like _dismiss_stale_reviews
    # and ai_pr_review.slash.dismiss read "user"/"state"/"body" off of it.
    assert result[0]["user"]["login"] == "github-actions[bot]"


def test_list_prior_bot_reviews_excludes_pending_and_narrows_shape() -> None:
    reviews = [
        _review(1, "PENDING", body="draft, should never be selected"),
        _review(2, "APPROVED", body="submitted"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reviews)

    prov = _make_provider(handler)
    result = prov._list_prior_bot_reviews()

    assert [r["id"] for r in result] == [2]
    assert result[0] == {"id": 2, "state": "APPROVED", "body": "submitted"}


def test_list_prior_bot_reviews_returns_empty_on_error_list_bot_reviews_does_not() -> None:
    """Same failing endpoint, fetched through each public method: pins the
    one behavioral difference this refactor deliberately preserves. Uses a
    plain 500 with a non-transient-looking body (not 503/429/502/504, which
    RecordingClient's retry_transient() would retry and eventually raise
    RetryExhaustedError from instead of handing back a response) so the
    error is handled entirely inside `_list_reviews_paginated`'s own
    `resp.status_code >= 400` branch, same as the two dedicated
    strict-vs-lenient tests above."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="unavailable")

    prov_a = _make_provider(handler)
    assert prov_a._list_prior_bot_reviews() == []
    assert prov_a._errors == []  # strict path never touches self._errors

    prov_b = _make_provider(handler)
    assert prov_b.list_bot_reviews() == []
    assert len(prov_b._errors) == 1  # non-strict path records the failure


# ---------------------------------------------------------------------------
# Consolidation guarantee: given the SAME underlying review list, both walks
# lead select_canonical() to the same review.
# ---------------------------------------------------------------------------


def test_both_walks_select_the_same_canonical_review_given_the_same_reviews() -> None:
    """No PENDING reviews and no fetch errors -- the one case where the two
    methods' independently-parameterized contracts (states filter, strict
    error handling) make no observable difference. Before this refactor, two
    separately hand-written pagination loops could in principle still drift
    apart here from an ordinary implementation bug (e.g. one loop losing the
    Link header reset, silently truncating a page); routing both through the
    one shared `_list_reviews_paginated` walk removes that class of risk."""
    reviews = [
        _review(1, "COMMENTED", body="oldest"),
        _review(3, "CHANGES_REQUESTED", body="newest, generated by [ai-pr-review]"),
        _review(2, "DISMISSED", body="middle"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reviews)

    prov_write = _make_provider(handler)
    write_side_reviews = prov_write.list_bot_reviews()

    prov_read = _make_provider(handler)
    read_side_reviews = prov_read._list_prior_bot_reviews()

    write_canonical = select_canonical(write_side_reviews)
    read_canonical = select_canonical(read_side_reviews)

    assert write_canonical is not None
    assert read_canonical is not None
    assert write_canonical.review_id == read_canonical.review_id == 3
    assert write_canonical.state == read_canonical.state == "CHANGES_REQUESTED"
    assert write_canonical.body == read_canonical.body
