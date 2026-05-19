"""GitLabProvider tests: summary upsert, skip, last-SHA, watermark, auth."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.gitlab import (
    GitLabConfig,
    GitLabProvider,
    _auth_header,
    _project_path_segment,
)
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, SUMMARY_MARKER_PREFIX


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    project_id: str = "42",
) -> tuple[GitLabProvider, _Recorder]:
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
    http = httpx.Client(transport=transport, base_url="https://gitlab.com/api/v4")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    config = GitLabConfig(
        project_id_or_path=project_id,
        mr_iid=7,
        token="glpat-testtokenvalue",
        diff_base_sha="abc1234567",
        bot_username="ai-bot",
    )
    return GitLabProvider(config=config, client=client), rec


# ---------------------------------------------------------------------------
# Auth header detection
# ---------------------------------------------------------------------------


def test_auth_header_glpat() -> None:
    name, value = _auth_header("glpat-foo")
    assert name == "PRIVATE-TOKEN"
    assert value == "glpat-foo"


def test_auth_header_glcbt() -> None:
    name, value = _auth_header("glcbt-foo")
    assert name == "JOB-TOKEN"
    assert value == "glcbt-foo"


def test_auth_header_oauth_fallback() -> None:
    name, value = _auth_header("opaque-oauth-token")
    assert name == "Authorization"
    assert value == "Bearer opaque-oauth-token"


# ---------------------------------------------------------------------------
# Project path segment encoding
# ---------------------------------------------------------------------------


def test_project_segment_numeric_passthrough() -> None:
    assert _project_path_segment("12345") == "12345"


def test_project_segment_path_encodes_slashes() -> None:
    assert _project_path_segment("group/project") == "group%2Fproject"


def test_project_segment_subgroup_encodes() -> None:
    assert _project_path_segment("group/subgroup/project") == "group%2Fsubgroup%2Fproject"


# ---------------------------------------------------------------------------
# get_last_reviewed_sha
# ---------------------------------------------------------------------------


_VALID_SHA = "abc1234def5678abc1234def5678abc1234def56"


def test_get_last_reviewed_sha_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None


def test_get_last_reviewed_sha_extracts_from_first_desc() -> None:
    notes = [
        {"id": 99, "body": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nlatest"},
        {"id": 1, "body": f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nold"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=notes)

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_get_summary_body_returns_none_when_no_note() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.get_summary_body() is None


def test_get_summary_body_returns_first_desc_note_body() -> None:
    stored = f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\n## Summary\n\n<details>...</details>"
    notes = [
        {"id": 99, "body": stored},
        {"id": 1, "body": f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nold"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=notes)

    prov, _ = _make_provider(handler)
    assert prov.get_summary_body() == stored


def test_get_summary_body_returns_none_on_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    prov, _ = _make_provider(handler)
    assert prov.get_summary_body() is None


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
    assert result.comment_id == 99
    body = rec.calls[-1][2]["body"]
    assert SUMMARY_MARKER_PREFIX in body
    assert _VALID_SHA in body


def test_post_summary_updates_existing() -> None:
    existing = {"id": 55, "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nold"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[existing])
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## new", _VALID_SHA)
    assert result.updated is True
    assert result.comment_id == 55
    put_call = next(c for c in rec.calls if c[0] == "PUT")
    assert "/notes/55" in put_call[1]


def test_post_summary_deletes_duplicates() -> None:
    deletes: list[str] = []
    existing = [
        {"id": 10, "body": f"{SUMMARY_MARKER_PREFIX} -->\nA"},
        {"id": 11, "body": f"{SUMMARY_MARKER_PREFIX} -->\nB"},
        {"id": 12, "body": f"{SUMMARY_MARKER_PREFIX} -->\nC"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=existing)
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
    prov, rec = _make_provider(lambda _r: httpx.Response(200, json=[]))
    result = prov.post_summary("   \n  ", _VALID_SHA)
    assert not result.ok
    assert result.error == "empty summary body"
    assert rec.calls == []


def test_post_summary_uses_path_encoded_project() -> None:
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    prov, _ = _make_provider(handler, project_id="my-group/my-project")
    prov.post_summary("hi", _VALID_SHA)
    assert all("my-group%2Fmy-project" in u for u in captured)


# ---------------------------------------------------------------------------
# post_skip_comment
# ---------------------------------------------------------------------------


def test_post_skip_comment_includes_inline_marker() -> None:
    prov, rec = _make_provider(lambda _r: httpx.Response(201, json={"id": 1}))
    result = prov.post_skip_comment("No diff.")
    assert result.ok
    body = rec.calls[-1][2]["body"]
    assert INLINE_MARKER in body
    assert "No diff." in body


# ---------------------------------------------------------------------------
# advance_sha_watermark
# ---------------------------------------------------------------------------


def test_advance_sha_watermark_updates_existing() -> None:
    existing = {"id": 7, "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nb"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[existing])
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    assert prov.advance_sha_watermark(_VALID_SHA) is True
    put = next(c for c in rec.calls if c[0] == "PUT")
    assert _VALID_SHA in put[2]["body"]
    assert "01d4ead4ee" not in put[2]["body"]


def test_advance_sha_watermark_no_existing() -> None:
    prov, _ = _make_provider(lambda _r: httpx.Response(200, json=[]))
    assert prov.advance_sha_watermark(_VALID_SHA) is False
