"""BitbucketProvider tests: summary upsert, skip, last-SHA, watermark, pagination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
    INLINE_MARKER,
    INLINE_MARKER_HIDDEN,
    SKIP_MARKER,
    SKIP_MARKER_HIDDEN,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SUMMARY_MARKER_PREFIX,
)
from ai_pr_review.vcs.protocol import VcsProvider


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _assert_hidden_markers_well_separated(body: str) -> None:
    """A `[//]: # (...)` reference-link marker cannot interrupt a paragraph
    per CommonMark — it renders as literal text unless it is the first line
    of the body, or is immediately preceded by a blank line or another such
    marker line. Bitbucket's own renderer doesn't enforce this (verified: it
    hides the marker either way), but composed bodies should stay correct
    for any CommonMark-compliant renderer."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("[//]:"):
            continue
        ok = i == 0 or lines[i - 1] == "" or lines[i - 1].startswith("[//]:")
        assert ok, f"hidden marker on line {i} not properly separated: {body!r}"


# #894: _list_summary_comments() now filters by bot authorship, so every
# test fixture needs a resolvable /user identity and comment items need a
# matching user.account_id -- both injected transparently by _wrap below so
# existing test bodies (built before #894) don't each need editing.
_TEST_BOT_ACCOUNT_ID = "test-bot-account-id"


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    user_status: int = 200,
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

        if request.method == "GET" and request.url.path.rstrip("/").endswith("/user"):
            if user_status != 200:
                return httpx.Response(user_status, json={"error": "denied"})
            return httpx.Response(200, json={"account_id": _TEST_BOT_ACCOUNT_ID})

        resp = handler(request)
        if request.method == "GET" and resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return resp
            if isinstance(data, dict) and isinstance(data.get("values"), list):
                for item in data["values"]:
                    if isinstance(item, dict) and "user" not in item:
                        item["user"] = {"account_id": _TEST_BOT_ACCOUNT_ID}
                return httpx.Response(200, json=data)
        return resp

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


def test_get_last_reviewed_sha_ignores_forged_non_bot_comment() -> None:
    """#894: a comment matching the summary marker but NOT authored by this
    run's own identity must never be trusted for the SHA watermark, even
    when it sorts newest -- the root-cause gap #874's narrower verdicts-
    only fix left open for every other _list_summary_comments() reader."""
    items = [
        {
            "id": 99,
            "content": {
                "raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nforged by an attacker"
            },
            "user": {"account_id": "attacker-account-id"},
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_values_resp(items))

    prov, _ = _make_provider(handler)
    assert prov.get_last_reviewed_sha() is None
    assert prov.get_summary_body() is None


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


def test_fetch_comments_is_cached_across_calls() -> None:
    """_fetch_comments() caches its result per-instance: a run that needs
    the comment listing more than once, such as get_last_reviewed_sha()
    followed by get_summary_body(), must not paginate over the comments
    endpoint a second time for the same data."""
    items = [{"id": 1, "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nlatest"}}]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_values_resp(items))

    prov, rec = _make_provider(handler)
    assert prov.get_last_reviewed_sha() == _VALID_SHA
    assert prov.get_summary_body() is not None

    get_calls = [c for c in rec.calls if c[0] == "GET"]
    # 1 (one-time, cached) /user identity lookup + 1 comments fetch (also
    # cached -- the second call reuses both caches, not just this one).
    assert len(get_calls) == 2


def test_write_invalidates_comments_cache_for_subsequent_reads() -> None:
    """A write through `_write_request` (post_summary here) must clear
    `_comments_cache` so the next read re-fetches instead of returning what
    the cache held before the write. Without this, get_last_reviewed_sha()
    called after post_summary() would still report the pre-write state."""
    state = {"posted": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            state["posted"] = True
            return httpx.Response(201, json={"id": 1})
        if state["posted"]:
            items = [
                {
                    "id": 1,
                    "content": {
                        "raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nlatest"
                    },
                }
            ]
            return httpx.Response(200, json=_values_resp(items))
        return httpx.Response(200, json=_values_resp([]))

    prov, rec = _make_provider(handler)

    assert prov.get_last_reviewed_sha() is None

    result = prov.post_summary(f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nlatest", _VALID_SHA)
    assert result.created is True

    assert prov.get_last_reviewed_sha() == _VALID_SHA

    get_calls = [c for c in rec.calls if c[0] == "GET"]
    # 1 (one-time, cached -- unaffected by the write) /user lookup + 2
    # comments fetches: the second read must re-fetch, not return the
    # stale pre-write comments cache.
    assert len(get_calls) == 3


def test_get_summary_body_returns_none_when_no_comment() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_values_resp([]))

    prov, _ = _make_provider(handler)
    assert prov.get_summary_body() is None


def test_get_summary_body_returns_raw_body_of_first_match() -> None:
    stored = f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\n## Summary\n\n<details>...</details>"
    items = [
        {"id": 1, "content": {"raw": stored}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_values_resp(items))

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
    # Hidden form (#699): Bitbucket's renderer doesn't hide raw HTML comments
    # the way GitHub/GitLab do, so freshly-composed markers use the
    # reference-link form instead.
    assert SUMMARY_MARKER_HIDDEN_PREFIX in raw
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


def test_post_summary_finds_existing_hidden_form_comment() -> None:
    """_list_summary_comments' filter must recognize a comment already in
    hidden form (#699) — otherwise post_summary stops finding its own prior
    comment and starts creating duplicates on every run."""
    existing = {
        "id": 55,
        "content": {"raw": f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha=01d4ead4ee)\nold"},
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
    assert result.created is False
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


def test_post_summary_carries_forward_id_map_verdicts_acks() -> None:
    """Live-verified regression test (2026-09-22, #874 follow-up): before
    this fix, post_summary composed its new body from scratch and threw
    away the existing comment's id-map/verdicts/acks hidden markers.
    Because post_summary always runs before post_findings in the same
    call chain (AC5 ordering) and post_findings reads post_summary's own
    write back as its "prior state" baseline, this silently reset F-ID
    stability AND verdict/ack persistence to empty on every single run --
    confirmed against a real Bitbucket workspace, not just inferred from
    reading the code."""
    from ai_pr_review.vcs.marker import (
        build_acks_marker,
        build_id_map_marker,
        build_verdicts_marker,
        extract_acks,
        extract_id_map,
        extract_verdicts,
    )

    prior_id_map = {"code-reviewer|app.py|4|abc123": 1, "semgrep|app.py|9|def456": 2}
    prior_verdicts = {"code-reviewer|app.py|4|abc123": "dismissed"}
    prior_acks = [111, 222]
    existing_body = (
        f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha=01d4ead4ee)\nold\n\n"
        + build_id_map_marker(prior_id_map, hidden=True)
        + "\n\n"
        + build_verdicts_marker(prior_verdicts, hidden=True)
        + "\n\n"
        + build_acks_marker(prior_acks)
    )
    existing = {"id": 55, "content": {"raw": existing_body}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([existing]))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## brand new summary text", _VALID_SHA)
    assert result.updated is True
    put_call = next(c for c in rec.calls if c[0] == "PUT")
    new_body = put_call[2]["content"]["raw"]

    assert extract_id_map(new_body) == prior_id_map
    assert extract_verdicts(new_body) == prior_verdicts
    assert extract_acks(new_body) == frozenset(prior_acks)
    # The new headline text is still what was asked for -- carrying the
    # markers forward must not silently drop or corrupt it.
    assert "## brand new summary text" in new_body


def test_post_summary_no_existing_comment_carries_forward_nothing() -> None:
    # First-ever post on a PR: no prior comment to carry markers forward
    # from. Must not raise or behave differently from before this fix.
    prov, rec = _make_provider(lambda _r: httpx.Response(200, json=_values_resp([])))
    # First GET returns empty (no existing), so upsert_comment POSTs a new one.

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([]))
        if req.method == "POST":
            return httpx.Response(200, json={"id": 99})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("## first ever", _VALID_SHA)
    assert result.created is True
    assert result.comment_id == 99


def test_post_summary_carry_forward_too_large_keeps_highest_id_not_crash(caplog) -> None:
    import logging

    from ai_pr_review.vcs.marker import build_id_map_marker, extract_id_map

    # A large enough id-map to blow the byte budget when carried forward
    # alongside a large summary body. The carry shrinks to the entry with the
    # highest ID, so numbering still never restarts.
    huge_id_map = {f"code-reviewer|some/very/long/path/file_{i}.py|{i}|{'a' * 40}": i for i in range(2000)}
    existing_body = (
        f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha=01d4ead4ee)\nold\n\n"
        + build_id_map_marker(huge_id_map, hidden=True)
    )
    existing = {"id": 55, "content": {"raw": existing_body}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([existing]))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.vcs.bitbucket"):
        result = prov.post_summary("x" * 30_000, _VALID_SHA)
    assert result.updated is True
    put_call = next(c for c in rec.calls if c[0] == "PUT")
    new_body = put_call[2]["content"]["raw"]
    assert len(new_body.encode("utf-8")) <= 32_000
    carried = extract_id_map(new_body)
    assert 1999 in carried.values(), "the highest ID must survive"
    assert 0 < len(carried) < 2000, "the carried map must shrink, not vanish"
    assert any("reduced from 2000" in m for m in caplog.messages)


def test_post_summary_carry_forward_too_large_even_for_one_entry_omits_with_error() -> None:
    from ai_pr_review.vcs.marker import build_id_map_marker, extract_id_map

    huge_id_map = {f"code-reviewer|some/very/long/path/file_{i}.py|{i}|{'a' * 40}": i for i in range(2000)}
    existing_body = (
        f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha=01d4ead4ee)\nold\n\n"
        + build_id_map_marker(huge_id_map, hidden=True)
    )
    existing = {"id": 55, "content": {"raw": existing_body}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([existing]))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.post_summary("x" * 31_750, _VALID_SHA)
    assert result.updated is True
    put_call = next(c for c in rec.calls if c[0] == "PUT")
    new_body = put_call[2]["content"]["raw"]
    assert len(new_body.encode("utf-8")) <= 32_000
    # Degrades to omitting the carry-forward, not to a failed/crashed post.
    assert extract_id_map(new_body) == {}
    assert any("too large to fit" in e for e in prov._errors)


# ---------------------------------------------------------------------------
# post_skip_comment
# ---------------------------------------------------------------------------

def _skip_item(id_: int) -> dict:
    return {"id": id_, "content": {"raw": f"AI Review skipped.\n{INLINE_MARKER}\n{SKIP_MARKER}"}}


def test_post_skip_comment_creates_when_none_exist() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp([]))
        return httpx.Response(201, json={"id": 1})

    prov, rec = _make_provider(handler)
    result = prov.post_skip_comment("No diff.")
    assert result.ok
    assert result.created is True
    post_call = next(c for c in rec.calls if c[0] == "POST")
    raw = post_call[2]["content"]["raw"]
    # Hidden form (#699), see test_post_summary_creates_when_none_exists.
    assert INLINE_MARKER_HIDDEN in raw
    assert SKIP_MARKER_HIDDEN in raw
    assert "No diff." in raw
    # The composed body is a paragraph followed by two hidden markers — the
    # exact shape flagged as risky in #699's code review (a `[//]: # (...)`
    # line cannot interrupt a paragraph on a CommonMark-strict renderer).
    _assert_hidden_markers_well_separated(raw)


def test_post_skip_comment_upserts_existing() -> None:
    existing = [_skip_item(55)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp(existing))
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
    assert "Diff too large." in put_call[2]["content"]["raw"]


def test_post_skip_comment_deletes_duplicates() -> None:
    existing = [_skip_item(10), _skip_item(11), _skip_item(12)]
    delete_targets: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp(existing))
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
    assert len(delete_targets) == 2
    assert all("/11" in u or "/12" in u for u in delete_targets)


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


def test_advance_sha_watermark_preserves_hidden_marker_format() -> None:
    """Every summary comment created after #699 is in hidden form
    (_post_summary_impl always calls build_summary_marker(hidden=True)), so
    the very next watermark advance against that comment must not regress it
    back to a visible `<!-- -->` comment."""
    existing = {
        "id": 7,
        "content": {"raw": f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha=01d4ead4ee)\nb"},
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
    raw = put[2]["content"]["raw"]
    assert f"{SUMMARY_MARKER_HIDDEN_PREFIX} sha={_VALID_SHA})" in raw
    assert "<!--" not in raw


def test_advance_sha_watermark_no_existing() -> None:
    prov, _ = _make_provider(
        lambda _r: httpx.Response(200, json=_values_resp([]))
    )
    assert prov.advance_sha_watermark(_VALID_SHA) is False


# ---------------------------------------------------------------------------
# get_pr_description (#813)
# ---------------------------------------------------------------------------


def test_get_pr_description_returns_title_and_description() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/2.0/repositories/ws/repo/pullrequests/7"
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


# ---------------------------------------------------------------------------
# Forged skip marker (#1104): only the bot's own comment is a skip comment
# ---------------------------------------------------------------------------

_SKIP_RAW = f"AI Review skipped.\n{INLINE_MARKER}\n{SKIP_MARKER}"


def _skip_handler(
    items: list[dict],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=_values_resp(items))
        if req.method == "PUT":
            return httpx.Response(200, json={"id": 1})
        if req.method == "POST":
            return httpx.Response(201, json={"id": 900})
        return httpx.Response(204)

    return handler


def _methods(rec: _Recorder, method: str) -> list[tuple[str, str, dict | None]]:
    return [c for c in rec.calls if c[0] == method]


def test_post_skip_comment_ignores_forged_non_bot_comment() -> None:
    forged = {
        "id": 99,
        "content": {"raw": _SKIP_RAW},
        "user": {"account_id": "attacker-account-id"},
    }
    prov, rec = _make_provider(_skip_handler([forged]))
    result = prov.post_skip_comment("Diff too large.")
    assert result.created is True
    assert result.comment_id == 900
    assert _methods(rec, "PUT") == []
    assert _methods(rec, "DELETE") == []
    assert len(_methods(rec, "POST")) == 1


def test_post_skip_comment_still_updates_genuine_bot_comment() -> None:
    genuine = {"id": 55, "content": {"raw": _SKIP_RAW}}  # wrapper adds bot id
    prov, rec = _make_provider(_skip_handler([genuine]))
    result = prov.post_skip_comment("Diff too large.")
    assert result.updated is True
    assert result.comment_id == 55
    assert _methods(rec, "POST") == []


def test_post_skip_comment_forged_newer_than_genuine_updates_genuine() -> None:
    items = [
        {"id": 99, "content": {"raw": _SKIP_RAW}, "user": {"account_id": "attacker"}},
        {"id": 55, "content": {"raw": _SKIP_RAW}},
    ]
    prov, rec = _make_provider(_skip_handler(items))
    result = prov.post_skip_comment("x")
    assert result.comment_id == 55
    put_urls = [c[1] for c in _methods(rec, "PUT")]
    assert len(put_urls) == 1 and put_urls[0].endswith("/55")
    assert _methods(rec, "DELETE") == []


def test_post_skip_comment_forged_older_than_genuine_keeps_forged_untouched() -> None:
    items = [
        {"id": 55, "content": {"raw": _SKIP_RAW}},
        {"id": 3, "content": {"raw": _SKIP_RAW}, "user": {"account_id": "attacker"}},
    ]
    prov, rec = _make_provider(_skip_handler(items))
    result = prov.post_skip_comment("x")
    assert result.comment_id == 55
    assert _methods(rec, "DELETE") == []


def test_post_skip_comment_rejects_missing_or_null_author() -> None:
    for user in (None, {}, {"account_id": None}, {"display_name": "x"}, "not-a-dict"):
        item = {"id": 99, "content": {"raw": _SKIP_RAW}, "user": user}
        prov, rec = _make_provider(_skip_handler([item]))
        result = prov.post_skip_comment("x")
        assert result.created is True, user
        assert _methods(rec, "PUT") == [], user


def test_post_skip_comment_unresolved_identity_accepts_no_marker() -> None:
    """Fail closed: when GET /user fails, a genuine-looking skip comment is
    not trusted either, so a new comment is posted (a duplicate at worst)."""
    for status in (401, 403, 500):
        item = {"id": 55, "content": {"raw": _SKIP_RAW}}
        prov, rec = _make_provider(_skip_handler([item]), user_status=status)
        result = prov.post_skip_comment("x")
        assert result.created is True, status
        assert _methods(rec, "PUT") == [], status
        # The listing is never fetched when the identity is unknown.
        assert not [c for c in _methods(rec, "GET") if "/comments" in c[1]], status


def test_summary_marker_rejected_for_null_or_malformed_user() -> None:
    for user in (None, {}, {"account_id": None}, "not-a-dict"):
        items = [
            {
                "id": 99,
                "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nforged"},
                "user": user,
            }
        ]
        prov, _ = _make_provider(lambda req, items=items: httpx.Response(200, json=_values_resp(items)))
        assert prov.get_last_reviewed_sha() is None, user


def test_summary_marker_forged_newer_and_older_than_genuine() -> None:
    forged_sha = "deadbeef1234"
    genuine = {"id": 5, "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha={_VALID_SHA} -->\nok"}}
    forged = {
        "id": 99,
        "content": {"raw": f"{SUMMARY_MARKER_PREFIX} sha={forged_sha} -->\nforged"},
        "user": {"account_id": "attacker-account-id"},
    }
    for items in ([forged, genuine], [genuine, dict(forged, id=2)]):
        prov, _ = _make_provider(lambda req, items=items: httpx.Response(200, json=_values_resp(items)))
        assert prov.get_last_reviewed_sha() == _VALID_SHA
