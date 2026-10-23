"""GitHubProvider tests: summary upsert, skip, last-SHA, watermark advance."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, SKIP_MARKER, SUMMARY_MARKER_PREFIX


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
# get_last_reviewed_sha / get_summary_body
# ---------------------------------------------------------------------------


_VALID_SHA = "abc1234def5678abc1234def5678abc1234def56"
_BOT = {"login": "github-actions[bot]"}
_FORGER = {"login": "mallory"}


def test_get_last_reviewed_sha_empty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None


def test_get_last_reviewed_sha_extracts_from_latest() -> None:
    comments = [
        {"id": 1, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} sha=deadbeefcafe1234 -->\nold"},
        {"id": 2, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nnew"},
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


def test_get_summary_body_returns_none_when_no_comment() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.get_summary_body() is None


def test_get_summary_body_returns_latest_comment_body() -> None:
    stored = f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\n## Summary\n\n<details>...</details>"
    comments = [
        {"id": 1, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} sha=aaa -->\nold"},
        {"id": 2, "user": _BOT, "body": stored},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

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
    assert result.updated is False
    assert result.comment_id == 99
    methods = [c[0] for c in rec.calls]
    assert methods == ["GET", "POST"]
    body = rec.calls[-1][2]["body"]
    assert SUMMARY_MARKER_PREFIX in body
    assert _VALID_SHA in body
    assert "## Summary" in body


def test_post_summary_updates_existing() -> None:
    existing = {"id": 55, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nold"}

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
        {"id": 10, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} -->\nA"},
        {"id": 11, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} -->\nB"},
        {"id": 12, "user": _BOT, "body": f"{SUMMARY_MARKER_PREFIX} -->\nC"},
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


def test_post_skip_comment_creates_when_none_exist() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("No changes to review.")
    assert result.ok
    assert result.created is True
    post_call = next(c for c in rec.calls if c[0] == "POST")
    body = post_call[2]["body"]
    assert INLINE_MARKER in body
    assert SKIP_MARKER in body
    assert "No changes to review." in body


def test_post_skip_comment_default_reason() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 2})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("")
    assert result.ok
    post_call = next(c for c in rec.calls if c[0] == "POST")
    assert "No changes to review." in post_call[2]["body"]


def test_post_skip_comment_upserts_existing() -> None:
    existing = [{"id": 55, "user": _BOT, "body": f"AI Review skipped.\n{INLINE_MARKER}\n{SKIP_MARKER}"}]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("Diff too large.")
    assert result.ok
    assert result.updated is True
    assert result.comment_id == 55
    patch_call = next(c for c in rec.calls if c[0] == "PATCH")
    assert "/issues/comments/55" in patch_call[1]
    assert "Diff too large." in patch_call[2]["body"]


def test_post_skip_comment_deletes_duplicates() -> None:
    existing = [
        {"id": 10, "user": _BOT, "body": f"skip\n{INLINE_MARKER}\n{SKIP_MARKER}"},
        {"id": 11, "user": _BOT, "body": f"skip\n{INLINE_MARKER}\n{SKIP_MARKER}"},
        {"id": 12, "user": _BOT, "body": f"skip\n{INLINE_MARKER}\n{SKIP_MARKER}"},
    ]
    delete_targets: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": 10})
        if req.method == "DELETE":
            delete_targets.append(str(req.url))
            return httpx.Response(204)
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_skip_comment("Too large.")
    assert result.updated is True
    assert result.comment_id == 10
    assert len(delete_targets) == 2
    assert all("/11" in u or "/12" in u for u in delete_targets)


# ---------------------------------------------------------------------------
# advance_sha_watermark
# ---------------------------------------------------------------------------


def test_advance_sha_watermark_updates_existing_marker() -> None:
    existing = {
        "id": 7,
        "user": _BOT,
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


def test_advance_sha_watermark_picks_oldest_marker_when_duplicates() -> None:
    """Consistency: post_summary keeps existing[0] (oldest) and deletes the
    rest, so advance_sha_watermark must also patch existing[0] — not
    existing[-1] — so the same comment is the survivor across both code
    paths. GitLab and Bitbucket both use existing[0] in both flows.
    """
    oldest = {
        "id": 7,
        "user": _BOT,
        "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nfirst",
    }
    newest = {
        "id": 99,
        "user": _BOT,
        "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nlater duplicate",
    }
    patched_ids: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[oldest, newest])
        if req.method == "PATCH":
            # Capture which comment id was patched (URL ends in /comments/<id>)
            patched_ids.append(int(str(req.url).rstrip("/").split("/")[-1]))
            return httpx.Response(200, json={"id": patched_ids[-1]})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    assert prov.advance_sha_watermark(_VALID_SHA) is True
    assert patched_ids == [7], (
        f"advance_sha_watermark must patch the OLDEST marker (id=7); patched={patched_ids}"
    )


def test_advance_sha_watermark_no_existing_returns_false() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    prov, _ = _make_provider(handler)
    assert prov.advance_sha_watermark(_VALID_SHA) is False


def test_advance_sha_watermark_rejects_invalid_sha() -> None:
    existing = {
        "id": 7,
        "user": _BOT,
        "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nbody",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[existing])

    prov, rec = _make_provider(handler)
    # invalid sha: non-hex
    assert prov.advance_sha_watermark("not-a-real-sha") is False
    # only the GET happened; no PATCH attempted
    assert all(c[0] != "PATCH" for c in rec.calls)


# ---------------------------------------------------------------------------
# get_pr_description (#813)
# ---------------------------------------------------------------------------


def test_get_pr_description_returns_title_and_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/o/r/pulls/7"
        return httpx.Response(200, json={"title": "Add widget", "body": "Fixes #1"})

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() == ("Add widget", "Fixes #1")


def test_get_pr_description_null_body_becomes_empty_string() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "Add widget", "body": None})

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() == ("Add widget", "")


def test_get_pr_description_http_error_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() is None
    assert prov._errors


def test_get_pr_description_missing_title_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"body": "no title field"})

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() is None


def test_get_pr_description_non_json_body_returns_none() -> None:
    """A malformed/non-JSON 2xx response must degrade to None (appended to
    self._errors), not raise -- matching gitlab.py/bitbucket.py's
    get_pr_description (review finding F1 on PR #817)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() is None
    assert prov._errors


# ---------------------------------------------------------------------------
# Forged markers from a non-bot author (CAP-6)
# ---------------------------------------------------------------------------

_FORGED_SHA = "f" * 40


def _forged_summary(cid: int, user: object = _FORGER) -> dict:
    c: dict = {"id": cid, "body": f"{SUMMARY_MARKER_PREFIX} sha={_FORGED_SHA} -->\nforged"}
    if user is not _MISSING:
        c["user"] = user
    return c


_MISSING = object()


def _forged_skip(cid: int, user: object = _FORGER) -> dict:
    c: dict = {"id": cid, "body": f"forged\n{INLINE_MARKER}\n{SKIP_MARKER}"}
    if user is not _MISSING:
        c["user"] = user
    return c


def _bot_summary(cid: int) -> dict:
    return {
        "id": cid,
        "user": _BOT,
        "body": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nreal",
    }


def test_forged_newer_marker_does_not_win_watermark() -> None:
    comments = [_bot_summary(1), _forged_summary(2)]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() == _VALID_SHA
    assert prov.get_summary_body() == comments[0]["body"]


def test_forged_older_marker_is_never_patched_or_deleted() -> None:
    comments = [_forged_summary(1), _bot_summary(2)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=comments)
        return httpx.Response(200, json={"id": 2})

    prov, rec = _make_provider(handler)
    result = prov.post_summary("fresh", _VALID_SHA)
    assert result.ok and result.updated is True
    assert result.comment_id == 2
    writes = [c for c in rec.calls if c[0] != "GET"]
    assert [c[0] for c in writes] == ["PATCH"]
    assert "/issues/comments/2" in writes[0][1]
    assert all(c[0] != "DELETE" for c in rec.calls)


def test_only_forged_marker_yields_none_and_new_post() -> None:
    comments = [_forged_summary(1)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=comments)
        return httpx.Response(201, json={"id": 99})

    prov, rec = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None
    assert prov.advance_sha_watermark(_VALID_SHA) is False
    result = prov.post_summary("fresh", _VALID_SHA)
    assert result.ok and result.created is True
    methods = [c[0] for c in rec.calls if c[0] != "GET"]
    assert methods == ["POST"]


def test_forged_skip_marker_is_not_patched_or_deleted() -> None:
    bot_skip = {
        "id": 2,
        "user": _BOT,
        "body": f"skip\n{INLINE_MARKER}\n{SKIP_MARKER}",
    }
    comments = [_forged_skip(1), bot_skip]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=comments)
        return httpx.Response(200, json={"id": 2})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("Too large.")
    assert result.ok and result.comment_id == 2
    writes = [c for c in rec.calls if c[0] != "GET"]
    assert [c[0] for c in writes] == ["PATCH"]
    assert "/issues/comments/2" in writes[0][1]


def test_only_forged_skip_marker_creates_own_comment() -> None:
    comments = [_forged_skip(1)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=comments)
        return httpx.Response(201, json={"id": 5})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("Too large.")
    assert result.ok and result.created is True
    assert [c[0] for c in rec.calls if c[0] != "GET"] == ["POST"]


def test_missing_null_or_loginless_user_is_rejected() -> None:
    comments = [
        _forged_summary(1, user=_MISSING),
        _forged_summary(2, user=None),
        _forged_summary(3, user={}),
        _forged_summary(4, user={"login": None}),
        _forged_summary(5, user={"id": 1}),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None
    assert prov.get_summary_body() is None


def test_lookalike_logins_are_rejected() -> None:
    comments = [
        _forged_summary(1, user={"login": "github-actions"}),
        _forged_summary(2, user={"login": "github-actions-fake"}),
        _forged_summary(3, user={"login": "github-actions[bot]x"}),
        _forged_skip(4, user={"login": "github-actions"}),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None
    assert prov._list_skip_comments() == []


def test_custom_bot_login_is_accepted() -> None:
    comment = {
        "id": 1,
        "user": {"login": "my-app[bot]"},
        "body": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nreal",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[comment])

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None  # default login rejects it
    custom = GitHubProvider(
        config=dataclasses.replace(prov.config, bot_login="my-app[bot]"),
        client=prov.client,
    )
    assert custom.get_last_reviewed_sha() == _VALID_SHA


def test_bot_login_comparison_ignores_case() -> None:
    provider, _ = _make_provider(
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "body": "<!-- ai-pr-review-summary sha=abc -->",
                    "user": {"login": "GitHub-Actions[bot]"},
                }
            ],
        )
    )
    assert len(provider._list_summary_comments()) == 1
