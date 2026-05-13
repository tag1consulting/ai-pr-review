"""BitbucketProvider tests: summary upsert, skip, last-SHA, watermark, pagination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, SUMMARY_MARKER_PREFIX
from ai_pr_review.vcs.protocol import VcsProvider


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[BitbucketProvider, _Recorder]:
    rec = _Recorder()

    def _wrap(request: httpx.Request) -> httpx.Response:
        import json

        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = {"_raw": request.content.decode("utf-8", errors="replace")}
        rec.calls.append((request.method, str(request.url), body))
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
    config = BitbucketConfig(
        workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t"
    )
    return BitbucketProvider(config=config, client=client), rec


_VALID_SHA = "abc1234def5678abc1234def5678abc1234def56"


def _values_resp(items: list[dict], next_url: str | None = None) -> dict:
    body: dict = {"values": items}
    if next_url:
        body["next"] = next_url
    return body


# ---------------------------------------------------------------------------
# get_last_reviewed_sha
# ---------------------------------------------------------------------------


def test_get_last_reviewed_sha_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_values_resp([]))

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None


def test_get_last_reviewed_sha_extracts_from_first() -> None:
    items = [
        {
            "id": 99,
            "content": {
                "raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nlatest"
            },
        },
        {"id": 1, "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nold"}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_values_resp(items))

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_get_last_reviewed_sha_paginates_via_next() -> None:
    page1 = _values_resp(
        [{"id": 5, "content": {"raw": "noise"}}],
        next_url="https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/7/comments?page=2",
    )
    page2 = _values_resp(
        [
            {
                "id": 7,
                "content": {
                    "raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->"
                },
            }
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if "page=2" in str(req.url):
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() == _VALID_SHA


# ---------------------------------------------------------------------------
# post_summary
# ---------------------------------------------------------------------------


def test_post_summary_creates_when_none_exists() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([]))
        if req.method == "POST":
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## Summary", _VALID_SHA)
    assert result.ok
    assert result.created is True
    assert result.comment_id == 99
    payload = rec.calls[-1][2]
    raw = payload["content"]["raw"]
    assert SUMMARY_MARKER_PREFIX in raw
    assert _VALID_SHA in raw


def test_post_summary_updates_existing() -> None:
    existing = {
        "id": 55,
        "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nold"},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([existing]))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## new", _VALID_SHA)
    assert result.updated is True
    assert result.comment_id == 55
    put_call = next(c for c in rec.calls if c[0] == "PUT")
    assert "/comments/55" in put_call[1]


def test_post_summary_deletes_duplicates() -> None:
    deletes: list[str] = []
    items = [
        {"id": 10, "content": {"raw": f"{SUMMARY_MARKER_PREFIX} -->\nA"}},
        {"id": 11, "content": {"raw": f"{SUMMARY_MARKER_PREFIX} -->\nB"}},
        {"id": 12, "content": {"raw": f"{SUMMARY_MARKER_PREFIX} -->\nC"}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp(items))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 10})
        if req.method == "DELETE":
            deletes.append(str(req.url))
            return httpx.Response(204)
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_summary("fresh", _VALID_SHA)
    assert result.updated is True
    assert len(deletes) == 2


def test_post_summary_empty_body_refuses() -> None:
    prov, rec = _make_provider(lambda _r: httpx.Response(200, json=_values_resp([])))
    result = prov.post_summary("   \n  ", _VALID_SHA)
    assert not result.ok
    assert result.error == "empty summary body"
    assert rec.calls == []


# ---------------------------------------------------------------------------
# post_skip_comment
# ---------------------------------------------------------------------------


def test_post_skip_comment_includes_inline_marker() -> None:
    prov, rec = _make_provider(lambda _r: httpx.Response(201, json={"id": 1}))
    result = prov.post_skip_comment("No diff.")
    assert result.ok
    raw = rec.calls[-1][2]["content"]["raw"]
    assert INLINE_MARKER in raw
    assert "No diff." in raw


# ---------------------------------------------------------------------------
# advance_sha_watermark
# ---------------------------------------------------------------------------


def test_advance_sha_watermark_updates_existing() -> None:
    existing = {
        "id": 7,
        "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nb"},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([existing]))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    assert prov.advance_sha_watermark(_VALID_SHA) is True
    put = next(c for c in rec.calls if c[0] == "PUT")
    assert _VALID_SHA in put[2]["content"]["raw"]


def test_advance_sha_watermark_no_existing() -> None:
    prov, _ = _make_provider(
        lambda _r: httpx.Response(200, json=_values_resp([]))
    )
    assert prov.advance_sha_watermark(_VALID_SHA) is False


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_bitbucket_provider_satisfies_protocol() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=_values_resp([])))
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    prov = BitbucketProvider(
        config=BitbucketConfig(
            workspace="w", repo_slug="r", pr_id=1, email="x@y", api_token="t"
        ),
        client=client,
    )
    assert isinstance(prov, VcsProvider)
