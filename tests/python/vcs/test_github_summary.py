"""GitHubProvider tests: summary upsert, skip, last-SHA, watermark advance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, SUMMARY_MARKER_PREFIX


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[GitHubProvider, _Recorder]:
    rec = _Recorder()

    def _wrap(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                import json

                body = json.loads(request.content)
            except Exception:
                body = {"_raw": request.content.decode("utf-8", errors="replace")}
        rec.calls.append((request.method, str(request.url), body))
        return handler(request)

    transport = httpx.MockTransport(_wrap)
    http = httpx.Client(transport=transport, base_url="https://api.github.com")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=7, token="t")
    return GitHubProvider(config=config, client=client), rec


# ---------------------------------------------------------------------------
# get_last_reviewed_sha
# ---------------------------------------------------------------------------


_VALID_SHA = "abc1234def5678abc1234def5678abc1234def56"


def test_get_last_reviewed_sha_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None


def test_get_last_reviewed_sha_extracts_from_latest() -> None:
    comments = [
        {"id": 1, "body": f"{SUMMARY_MARKER_PREFIX} sha=deadbeefcafe1234 -->\nold"},
        {"id": 2, "body": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nnew"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_get_last_reviewed_sha_handles_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    prov, _ = _make_provider(handler)
    # 500 is non-transient; provider should record error and return None.
    assert prov.get_last_reviewed_sha() is None


# ---------------------------------------------------------------------------
# post_summary
# ---------------------------------------------------------------------------


def test_post_summary_creates_when_none_exists() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[])
        if req.method == "POST":
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## Summary", _VALID_SHA)
    assert result.ok
    assert result.created is True
    assert result.updated is False
    assert result.comment_id == 99
    methods = [c[0] for c in rec.calls]
    assert methods == ["GET", "POST"]
    body = rec.calls[-1][2]["body"]
    assert SUMMARY_MARKER_PREFIX in body
    assert _VALID_SHA in body
    assert "## Summary" in body


def test_post_summary_updates_existing() -> None:
    existing = {"id": 55, "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nold"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[existing])
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## new summary", _VALID_SHA)
    assert result.ok
    assert result.updated is True
    assert result.created is False
    assert result.comment_id == 55
    # PATCH URL should hit issues/comments/55
    patch_call = next(c for c in rec.calls if c[0] == "PATCH")
    assert "/issues/comments/55" in patch_call[1]


def test_post_summary_deletes_duplicates() -> None:
    calls = {"delete_targets": []}
    existing = [
        {"id": 10, "body": f"{SUMMARY_MARKER_PREFIX} -->\nA"},
        {"id": 11, "body": f"{SUMMARY_MARKER_PREFIX} -->\nB"},
        {"id": 12, "body": f"{SUMMARY_MARKER_PREFIX} -->\nC"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": 10})
        if req.method == "DELETE":
            calls["delete_targets"].append(str(req.url))
            return httpx.Response(204)
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_summary("fresh", _VALID_SHA)
    assert result.updated is True
    assert result.comment_id == 10
    assert len(calls["delete_targets"]) == 2
    assert all("/11" in u or "/12" in u for u in calls["delete_targets"])


def test_post_summary_empty_body_refuses() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, rec = _make_provider(handler)
    result = prov.post_summary("   \n  ", _VALID_SHA)
    assert not result.ok
    assert result.error == "empty summary body"
    assert rec.calls == []  # no API call made


def test_post_summary_api_error_returns_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(422, text="unprocessable")

    prov, _ = _make_provider(handler)
    result = prov.post_summary("hi", _VALID_SHA)
    assert not result.ok
    assert "HTTP 422" in (result.error or "")


# ---------------------------------------------------------------------------
# post_skip_comment
# ---------------------------------------------------------------------------


def test_post_skip_comment_includes_inline_marker() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("No changes to review.")
    assert result.ok
    assert result.created is True
    body = rec.calls[-1][2]["body"]
    assert INLINE_MARKER in body
    assert "No changes to review." in body


def test_post_skip_comment_default_reason() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 2})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("")
    assert result.ok
    body = rec.calls[-1][2]["body"]
    assert "No changes to review." in body


# ---------------------------------------------------------------------------
# advance_sha_watermark
# ---------------------------------------------------------------------------


def test_advance_sha_watermark_updates_existing_marker() -> None:
    existing = {
        "id": 7,
        "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nbody",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[existing])
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    assert prov.advance_sha_watermark(_VALID_SHA) is True
    patch_call = next(c for c in rec.calls if c[0] == "PATCH")
    new_body = patch_call[2]["body"]
    assert _VALID_SHA in new_body
    assert "olddeadbee" not in new_body


def test_advance_sha_watermark_no_existing_returns_false() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.advance_sha_watermark(_VALID_SHA) is False


def test_advance_sha_watermark_rejects_invalid_sha() -> None:
    existing = {
        "id": 7,
        "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nbody",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[existing])

    prov, rec = _make_provider(handler)
    # invalid sha: non-hex
    assert prov.advance_sha_watermark("not-a-real-sha") is False
    # only the GET happened; no PATCH attempted
    assert all(c[0] != "PATCH" for c in rec.calls)
