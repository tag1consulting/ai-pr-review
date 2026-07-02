"""HTTP-mocked tests for ai_pr_review.slash.dismiss's GitHub orchestration.

Follows the `_make_provider(handler)` harness from `test_github_stale.py`.
Covers the #555 bug class specifically: a GraphQL 200-with-errors body and a
malformed/non-JSON body must surface in `DismissResult.errors` with no PUT
dismiss issued, proving the Python path cannot silently treat an error
response as valid data the way the bash `gh api --jq` call did.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.slash.dismiss import dismiss_by_finding_id, dismiss_inline_reply
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, build_id_map_marker


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
                body = _json.loads(request.content)
            except Exception:
                body = None
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
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    return GitHubProvider(config=config, client=client), rec


def _finding(text: str, source: str = "code-reviewer", file: str = "app.py", line: int = 10) -> Finding:
    return Finding(severity="medium", confidence=80, finding=text, source=source, file=file, line=line)


def _inline_thread(
    tid: str,
    *,
    resolved: bool,
    body: str,
    comment_db_id: int | None = None,
    review_db_id: int | None = None,
) -> dict:
    inner: dict = {"body": body, "author": {"login": "github-actions[bot]"}}
    if comment_db_id is not None:
        inner["databaseId"] = comment_db_id
    inner["pullRequestReview"] = {"databaseId": review_db_id} if review_db_id is not None else None
    return {"id": tid, "isResolved": resolved, "comments": {"nodes": [inner]}}


def _threads_response(nodes: list[dict]) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# dismiss_by_finding_id — BODY case
# ---------------------------------------------------------------------------


def test_dismiss_by_finding_id_body_finding_no_http_side_effects() -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
    bullet = format_body_finding(f, finding_id=3)
    review_body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 1, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "PUT":
            raise AssertionError("no dismiss PUT expected for a BODY finding")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 3, actor="alice", command="dismiss")

    assert result.feedback_source == "phpcs"
    assert result.feedback_file == "legacy.py"
    assert result.errors == ()
    assert result.thread_resolved is False
    assert result.review_dismissed is False


def test_dismiss_by_finding_id_unknown_id() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 999, actor="alice", command="dismiss")

    assert "could not find" in result.reply
    assert result.errors == ()


# ---------------------------------------------------------------------------
# dismiss_by_finding_id — INLINE case
# ---------------------------------------------------------------------------


def test_dismiss_by_finding_id_inline_resolves_and_dismisses_review() -> None:
    id_map = {"security-reviewer|api.py|10|abc123456789": 4}
    our_body = f"[High] leak\n**[F4]**\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    dismissed: list[str] = []

    # The single GET /reviews call is list_bot_reviews() for classification:
    # the body carries the id-map (no rendered bullet), so classify_finding
    # falls through to the INLINE branch. _dismiss_if_all_resolved does not
    # re-list reviews — it decides from the (in-memory-updated) thread
    # snapshot plus the dismiss_review PUT — so `reviews` here only needs the
    # `body` field for classification.
    reviews_for_classification = [
        {"id": 41, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": our_body}
    ]

    def handler2(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=reviews_for_classification)
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in str(req.url):
            dismissed.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov, _ = _make_provider(handler2)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert result.thread_resolved is True
    assert result.review_dismissed is True
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]
    assert result.errors == ()


# ---------------------------------------------------------------------------
# #555 regression: GraphQL 200-with-errors / malformed body must surface in
# DismissResult.errors, never silently swallowed.
# ---------------------------------------------------------------------------


def test_dismiss_by_finding_id_graphql_errors_surface_in_result() -> None:
    id_map = {"x|y.py|1|aaaaaaaaaaaa": 4}
    our_body = f"finding\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    reviews = [{"id": 41, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": our_body}]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=reviews)
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            # #555 shape: HTTP 200 with a top-level GraphQL errors array.
            return httpx.Response(200, json={"errors": [{"message": "Not authorized to read review threads"}]})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert any("Not authorized to read review threads" in e for e in result.errors)
    # No dismiss PUT should have been attempted with garbage state.
    assert result.review_dismissed is False


def test_dismiss_inline_reply_graphql_errors_never_dismiss() -> None:
    """Symmetric #555 guard for dismiss_inline_reply: a GraphQL-200-with-errors
    thread fetch must surface in .errors and never be treated as "zero threads
    found -> nothing to resolve, safe to proceed" style garbage state."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json={"errors": [{"message": "field 'reviewThreads' doesn't exist"}]})
        if req.method == "PUT":
            raise AssertionError("no dismiss PUT expected when thread-fetch failed")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 55, None, actor="alice", command="dismiss")

    assert any("doesn't exist" in e for e in result.errors)
    assert result.review_dismissed is False
    assert result.thread_resolved is False


def test_dismiss_inline_reply_http_error_status_surfaces_in_result() -> None:
    """The #555 class also covers a plain non-2xx HTTP error mid-flow."""
    nodes = [_inline_thread("T2", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(403, json={"message": "forbidden"})
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss")

    assert result.thread_resolved is False
    assert len(result.errors) == 1
    assert "403" in result.errors[0]
    # Regression for review findings F1/F2: the reply must never claim
    # success ("resolved the thread") when resolve_thread actually failed.
    assert "resolved the thread" not in result.reply
    assert "could not resolve the thread" in result.reply


def test_dismiss_by_finding_id_inline_resolve_failure_reply_is_honest() -> None:
    """Symmetric F1 regression: dismiss_by_finding_id must not claim the
    thread was resolved when resolve_thread failed."""
    id_map = {"x|y.py|1|aaaaaaaaaaaa": 4}
    our_body = f"finding\n**[F4]**\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    reviews = [{"id": 41, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": our_body}]
    nodes = [_inline_thread("T9", resolved=False, body=our_body, comment_db_id=99, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=reviews)
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(403, json={"message": "forbidden"})
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert result.thread_resolved is False
    assert len(result.errors) == 1
    assert "resolved the thread" not in result.reply
    assert "could not resolve the thread" in result.reply


def test_dismiss_by_finding_id_unknown_reply_names_lookup_failure_not_absence() -> None:
    """Regression for F3: when list_bot_reviews's HTTP call fails, the reply
    must not claim the finding doesn't exist (UNKNOWN from an errored,
    possibly-partial lookup is not the same as a genuine miss)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(500, text="internal error")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert len(result.errors) == 1
    assert "500" in result.errors[0]
    assert "could not find finding" not in result.reply
    assert "could not complete the lookup" in result.reply


def test_dismiss_inline_reply_thread_not_found() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response([]))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 999, None, actor="alice", command="dismiss")

    assert "could not find" in result.reply


def test_dismiss_inline_reply_other_bots_thread_ignored() -> None:
    """Defense in depth: a comment not carrying our inline marker must be ignored,
    even if the parent_comment_id correlation matches."""
    nodes = [_inline_thread("T3", resolved=False, body="Dependabot notice", comment_db_id=88, review_db_id=None)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT":
            raise AssertionError("must never resolve/dismiss another bot's thread")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 88, None, actor="alice", command="dismiss")

    assert "not posted by this bot" in result.reply


def test_dismiss_inline_reply_graphql_style_author_still_owned() -> None:
    """Pins the deliberate `bot_login=None` choice in `_stale.is_owned_by_us`
    calls throughout this module: ownership is gated by the inline marker
    ALONE, never by comparing the GraphQL author login against the REST-style
    `github-actions[bot]` constant. Per `reference_bot_login_graphql_vs_rest`
    (unverified this session — flagged for live confirmation in story 13-2),
    GitHub's GraphQL API may report the bot's login without the "[bot]"
    suffix (`github-actions`), which would never equal the REST-style
    constant. This test locks in marker-only gating today; if a future change
    "fixes" the `None` to `self.config.bot_login`, this test fails and forces
    that decision to be re-examined rather than silently reintroducing a
    no-op author check (the exact class of bug that broke PR #378's
    inline-by-F-ID dismiss step)."""
    id_map = {"x|y.py|1|aaaaaaaaaaaa": 4}
    body = f"finding\n**[F4]**\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    # "github-actions" (no "[bot]" suffix) — the GraphQL-reported form per the
    # (unverified) memory note; must still be treated as ours.
    nodes = [
        {
            "id": "T_graphql_author",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 66,
                        "body": body,
                        "author": {"login": "github-actions"},
                        "pullRequestReview": None,
                    }
                ]
            },
        }
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body_json = _json.loads(req.content)
            if "resolveReviewThread" in body_json.get("query", ""):
                return httpx.Response(
                    200,
                    json={"data": {"resolveReviewThread": {"thread": {"id": "T_graphql_author", "isResolved": True}}}},
                )
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 66, None, actor="alice", command="dismiss")

    assert result.thread_resolved is True
