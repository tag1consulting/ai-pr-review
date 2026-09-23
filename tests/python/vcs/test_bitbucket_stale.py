"""BitbucketProvider.resolve_stale — marker-gated duplicate cleanup."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import SUMMARY_MARKER_HIDDEN_PREFIX, SUMMARY_MARKER_PREFIX

# #894: _list_summary_comments() now filters by bot authorship, so every
# test needs a resolvable /user identity -- injected transparently here so
# existing test bodies (built before #894) don't each need editing.
# _comment() below defaults each comment's user.account_id to match.
_TEST_BOT_ACCOUNT_ID = "test-bot-account-id"


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> BitbucketProvider:
    def _wrap(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.rstrip("/").endswith("/user"):
            return httpx.Response(200, json={"account_id": _TEST_BOT_ACCOUNT_ID})
        return handler(request)

    transport = httpx.MockTransport(_wrap)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    return BitbucketProvider(
        config=BitbucketConfig(
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t"
        ),
        client=client,
    )


def _comment(cid: int, body: str, *, account_id: str = _TEST_BOT_ACCOUNT_ID) -> dict:
    return {"id": cid, "content": {"raw": body}, "user": {"account_id": account_id}}


def _values(items: list[dict]) -> dict:
    return {"values": items}


def test_resolve_stale_no_duplicates_no_op() -> None:
    items = [
        _comment(100, f"{SUMMARY_MARKER_PREFIX} sha=abc1234 -->\nonly one"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values(items))
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 0


def test_resolve_stale_deletes_duplicates_keeping_first() -> None:
    items = [
        _comment(100, f"{SUMMARY_MARKER_PREFIX} sha=abc1234 -->\nlatest"),
        _comment(99, f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nmiddle"),
        _comment(98, f"{SUMMARY_MARKER_PREFIX} -->\nold"),
    ]
    deletes: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values(items))
        if req.method == "DELETE":
            url = str(req.url)
            cid = int(url.rsplit("/", 1)[-1])
            deletes.append(cid)
            return httpx.Response(204)
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 2
    assert sorted(deletes) == [98, 99]


def test_resolve_stale_never_deletes_forged_non_bot_comment() -> None:
    """#894: a marker-matching comment not authored by this run's own
    identity must be invisible to resolve_stale entirely -- neither kept
    as the canonical one nor deleted as a "duplicate"."""
    items = [
        _comment(100, f"{SUMMARY_MARKER_PREFIX} sha=abc1234 -->\nreal", account_id=_TEST_BOT_ACCOUNT_ID),
        _comment(99, f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nforged", account_id="attacker-account-id"),
    ]
    deletes: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values(items))
        if req.method == "DELETE":
            url = str(req.url)
            deletes.append(int(url.rsplit("/", 1)[-1]))
            return httpx.Response(204)
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert deletes == []


def test_resolve_stale_deletes_duplicates_mixed_marker_formats() -> None:
    """A comment stamped with the hidden form (#699) must still be recognized
    as ours by the marker-gated dedup, whether it's the kept or deleted one."""
    items = [
        _comment(100, f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha=abc1234)\nlatest"),
        _comment(99, f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nmiddle (legacy form)"),
    ]
    deletes: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values(items))
        if req.method == "DELETE":
            cid = int(str(req.url).rsplit("/", 1)[-1])
            deletes.append(cid)
            return httpx.Response(204)
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 1
    assert deletes == [99]


def test_resolve_stale_skips_non_markered_comments() -> None:
    """Defense: list_summary_comments already filters by marker. But the
    is_owned_by_us(kind="summary") check is the second gate — if a non-summary
    comment somehow got into our list, it must NOT be deleted."""
    # Synthesize a malformed entry that survives the prefix substring scan but
    # lacks the actual summary marker regex.
    items = [
        _comment(100, f"{SUMMARY_MARKER_PREFIX} sha=abc1234 -->\nlatest"),
        # Body literally contains the prefix string but in a malformed way
        # (different closing). is_owned_by_us(kind=summary) requires the full
        # regex to match, so this entry is skipped.
        _comment(50, f"{SUMMARY_MARKER_PREFIX} but no closing"),
    ]
    deletes: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values(items))
        if req.method == "DELETE":
            cid = int(str(req.url).rsplit("/", 1)[-1])
            deletes.append(cid)
            return httpx.Response(204)
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 1
    assert deletes == []


def test_resolve_stale_records_delete_failures() -> None:
    items = [
        _comment(100, f"{SUMMARY_MARKER_PREFIX} sha=abc1234 -->\nA"),
        _comment(99, f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nB"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values(items))
        if req.method == "DELETE":
            return httpx.Response(403, text="forbidden")
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert len(result.errors) == 1
    assert "HTTP 403" in result.errors[0]
