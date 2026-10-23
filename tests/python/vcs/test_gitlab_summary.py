"""GitLabProvider tests: summary upsert, skip, last-SHA, watermark, auth."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

import httpx

from ai_pr_review.vcs.gitlab import (
    GitLabConfig,
    GitLabProvider,
    _auth_header,
    _project_path_segment,
)
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, SKIP_MARKER, SUMMARY_MARKER_PREFIX


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
        {"id": 99, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nlatest"},
        {"id": 1, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nold"},
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
        {"id": 99, "author": {"username": "ai-bot"}, "body": stored},
        {"id": 1, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} sha=cafe1234 -->\nold"},
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
    existing = {"id": 55, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nold"}

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
        {"id": 10, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} -->\nA"},
        {"id": 11, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} -->\nB"},
        {"id": 12, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} -->\nC"},
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


def test_post_skip_comment_creates_when_none_exist() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("No diff.")
    assert result.ok
    assert result.created is True
    post_call = next(c for c in rec.calls if c[0] == "POST")
    body = post_call[2]["body"]
    assert INLINE_MARKER in body
    assert SKIP_MARKER in body
    assert "No diff." in body


def test_post_skip_comment_upserts_existing() -> None:
    existing = [{"id": 55, "author": {"username": "ai-bot"}, "body": f"AI Review skipped.\n{INLINE_MARKER}\n{SKIP_MARKER}"}]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("Diff too large.")
    assert result.ok
    assert result.updated is True
    assert result.comment_id == 55
    put_call = next(c for c in rec.calls if c[0] == "PUT")
    assert "/55" in put_call[1]
    assert "Diff too large." in put_call[2]["body"]


def test_post_skip_comment_deletes_duplicates() -> None:
    existing = [
        {"id": 10, "author": {"username": "ai-bot"}, "body": f"skip\n{INLINE_MARKER}\n{SKIP_MARKER}"},
        {"id": 11, "author": {"username": "ai-bot"}, "body": f"skip\n{INLINE_MARKER}\n{SKIP_MARKER}"},
    ]
    delete_targets: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=existing)
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 10})
        if req.method == "DELETE":
            delete_targets.append(str(req.url))
            return httpx.Response(204)
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_skip_comment("Too large.")
    assert result.updated is True
    assert result.comment_id == 10
    assert len(delete_targets) == 1
    assert "/11" in delete_targets[0]


# ---------------------------------------------------------------------------
# advance_sha_watermark
# ---------------------------------------------------------------------------


def test_advance_sha_watermark_updates_existing() -> None:
    existing = {"id": 7, "author": {"username": "ai-bot"}, "body": f"{SUMMARY_MARKER_PREFIX} sha=01d4ead4ee -->\nb"}

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


# ---------------------------------------------------------------------------
# get_pr_description (#813)
# ---------------------------------------------------------------------------


def test_get_pr_description_returns_title_and_description() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v4/projects/42/merge_requests/7"
        return httpx.Response(
            200, json={"title": "Add widget", "description": "Fixes #1"}
        )

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() == ("Add widget", "Fixes #1")


def test_get_pr_description_null_description_becomes_empty_string() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "Add widget", "description": None})

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
        return httpx.Response(200, json={"description": "no title field"})

    prov, _ = _make_provider(handler)
    assert prov.get_pr_description() is None


# ---------------------------------------------------------------------------
# Forged marker (#1104): only the bot's own note carries a trusted marker
# ---------------------------------------------------------------------------

_SKIP_BODY = f"AI Review skipped.\n{INLINE_MARKER}\n{SKIP_MARKER}"


def _summary_body(sha: str) -> str:
    return f"{SUMMARY_MARKER_PREFIX} sha={sha} -->\nbody"


def _forged(id_: int, body: str, username: object = "attacker") -> dict:
    return {"id": id_, "author": {"username": username}, "body": body}


def _genuine(id_: int, body: str) -> dict:
    return {"id": id_, "author": {"username": "ai-bot"}, "body": body}


def _notes_handler(
    notes: list[dict],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=notes)
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 1})
        if req.method == "POST":
            return httpx.Response(201, json={"id": 900})
        return httpx.Response(204)

    return handler


def _calls(rec: _Recorder, method: str) -> list[tuple[str, str, dict | None]]:
    return [c for c in rec.calls if c[0] == method]


def test_get_last_reviewed_sha_ignores_forged_non_bot_note() -> None:
    prov, _ = _make_provider(_notes_handler([_forged(99, _summary_body(_VALID_SHA))]))
    assert prov.get_last_reviewed_sha() is None
    assert prov.get_summary_body() is None


def test_genuine_bot_note_still_gives_watermark() -> None:
    prov, _ = _make_provider(_notes_handler([_genuine(5, _summary_body(_VALID_SHA))]))
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_forged_newer_than_genuine_returns_genuine_sha() -> None:
    notes = [
        _forged(99, _summary_body("deadbeef1234")),
        _genuine(5, _summary_body(_VALID_SHA)),
    ]
    prov, _ = _make_provider(_notes_handler(notes))
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_forged_older_than_genuine_returns_genuine_sha() -> None:
    notes = [
        _genuine(5, _summary_body(_VALID_SHA)),
        _forged(2, _summary_body("deadbeef1234")),
    ]
    prov, _ = _make_provider(_notes_handler(notes))
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_missing_null_or_malformed_author_is_rejected() -> None:
    body = _summary_body(_VALID_SHA)
    variants: list[dict] = [
        {"id": 1, "body": body},
        {"id": 1, "author": None, "body": body},
        {"id": 1, "author": {}, "body": body},
        {"id": 1, "author": {"username": None}, "body": body},
        {"id": 1, "author": "ai-bot", "body": body},
        {"id": 1, "author": {"username": 7}, "body": body},
    ]
    for note in variants:
        prov, _ = _make_provider(_notes_handler([note]))
        assert prov.get_last_reviewed_sha() is None, note
        prov, _ = _make_provider(_notes_handler([{**note, "body": _SKIP_BODY}]))
        assert prov._list_skip_notes() == [], note


def test_username_match_ignores_case() -> None:
    note = {"id": 5, "author": {"username": "AI-Bot"}, "body": _summary_body(_VALID_SHA)}
    prov, _ = _make_provider(_notes_handler([note]))
    assert prov.get_last_reviewed_sha() == _VALID_SHA


def test_post_summary_does_not_update_forged_note() -> None:
    prov, rec = _make_provider(_notes_handler([_forged(99, _summary_body("deadbeef1234"))]))
    result = prov.post_summary("hello", _VALID_SHA)
    assert result.created is True
    assert _calls(rec, "PUT") == []
    assert _calls(rec, "DELETE") == []


def test_advance_sha_watermark_ignores_forged_note() -> None:
    prov, rec = _make_provider(_notes_handler([_forged(99, _summary_body("deadbeef1234"))]))
    assert prov.advance_sha_watermark(_VALID_SHA) is False
    assert _calls(rec, "PUT") == []


def test_post_skip_comment_ignores_forged_note() -> None:
    prov, rec = _make_provider(_notes_handler([_forged(99, _SKIP_BODY)]))
    result = prov.post_skip_comment("Diff too large.")
    assert result.created is True
    assert _calls(rec, "PUT") == []
    assert _calls(rec, "DELETE") == []


def test_post_skip_comment_forged_newer_updates_genuine() -> None:
    notes = [_forged(99, _SKIP_BODY), _genuine(55, _SKIP_BODY)]
    prov, rec = _make_provider(_notes_handler(notes))
    result = prov.post_skip_comment("x")
    assert result.comment_id == 55
    assert [c[1].endswith("/55") for c in _calls(rec, "PUT")] == [True]
    assert _calls(rec, "DELETE") == []


def _unresolved_provider(
    notes: list[dict], user_status: int, user_json: dict | None = None
) -> tuple[GitLabProvider, _Recorder]:
    base = _notes_handler(notes)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/user"):
            return httpx.Response(user_status, json=user_json or {})
        return base(req)

    prov, rec = _make_provider(handler)
    prov.config = replace(prov.config, bot_username=None)
    return prov, rec


def test_unresolved_identity_trusts_no_marker() -> None:
    """Fail closed: even a note that looks genuine is ignored when the bot
    username is unknown. The effect is a full review, not an incremental one."""
    notes = [_genuine(5, _summary_body(_VALID_SHA)), _genuine(6, _SKIP_BODY)]
    for status, body in ((401, None), (403, None), (500, None), (200, {"id": 3})):
        prov, rec = _unresolved_provider(notes, status, body)
        assert prov.get_last_reviewed_sha() is None, status
        assert prov.get_summary_body() is None, status
        assert prov._list_skip_notes() == [], status
        assert prov.advance_sha_watermark(_VALID_SHA) is False, status
        assert _calls(rec, "PUT") == [], status


def test_username_resolved_from_user_endpoint_when_not_configured() -> None:
    notes = [_forged(99, _summary_body("deadbeef1234")), _genuine(5, _summary_body(_VALID_SHA))]
    prov, _ = _unresolved_provider(notes, 200, {"username": "ai-bot"})
    assert prov.get_last_reviewed_sha() == _VALID_SHA
