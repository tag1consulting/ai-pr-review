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
from ai_pr_review.slash.dismiss import (
    context_from_parent_comment,
    dismiss_by_finding_id,
    dismiss_inline_reply,
    resolve_only,
)
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider, _build_inline_comment_body
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
    """Regression guard for issue #562's fix: a CHANGES_REQUESTED review must
    still dismiss correctly once the review-state check is in place."""
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
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=reviews_for_classification)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            dismissed.append(url)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov, _ = _make_provider(handler2)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert result.thread_resolved is True
    assert result.review_dismissed is True
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]
    assert result.errors == ()


# ---------------------------------------------------------------------------
# Issue #562: verify review state before a dismiss PUT (shared helper)
# ---------------------------------------------------------------------------


def test_dismiss_by_finding_id_skips_dismiss_when_review_already_dismissed() -> None:
    """The defining fix for #562: a review that is already DISMISSED (or
    APPROVED/COMMENTED) with zero remaining unresolved threads must NOT
    trigger a dismiss PUT at all -- a clean, silent no-op, not a wasted API
    call GitHub would reject."""
    id_map = {"security-reviewer|api.py|10|abc123456789": 4}
    our_body = f"[High] leak\n**[F4]**\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    reviews_for_classification = [
        {"id": 41, "state": "DISMISSED", "user": {"login": "github-actions[bot]"}, "body": our_body}
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "DISMISSED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=reviews_for_classification)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            raise AssertionError("must not issue a dismiss PUT against a non-CHANGES_REQUESTED review")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert result.thread_resolved is True
    assert result.review_dismissed is False
    assert result.errors == ()


def test_dismiss_by_finding_id_state_fetch_failure_fails_closed() -> None:
    """A state-fetch HTTP error must skip the dismiss (fail closed) and
    surface an error -- never proceed to dismiss on unverifiable state."""
    id_map = {"security-reviewer|api.py|10|abc123456789": 4}
    our_body = f"[High] leak\n**[F4]**\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    reviews_for_classification = [
        {"id": 41, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": our_body}
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(404, text="Not Found")
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=reviews_for_classification)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            raise AssertionError("must not issue a dismiss PUT when review state could not be verified")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="dismiss")

    assert result.thread_resolved is True
    assert result.review_dismissed is False
    assert any("get_review_state" in e for e in result.errors)


def test_get_review_state_happy_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and str(req.url).endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    assert prov.get_review_state(41) == "CHANGES_REQUESTED"
    assert prov._errors == []


def test_get_review_state_http_error_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    prov, _ = _make_provider(handler)
    assert prov.get_review_state(41) is None
    assert any("get_review_state 41" in e for e in prov._errors)


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


# ---------------------------------------------------------------------------
# resolve_only — feedback-command's "resolve on success" step (story 13-4)
# ---------------------------------------------------------------------------


def test_resolve_only_resolves_without_dismissing() -> None:
    """The defining contract: resolve_only must NEVER issue a dismiss PUT,
    even though the thread's owning review has a databaseId — unlike
    dismiss_inline_reply, this path has no dismissal semantics at all."""
    nodes = [_inline_thread("T1", resolved=False, body="our finding", comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT":
            raise AssertionError("resolve_only must never issue a dismiss PUT")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    resolved, errors = resolve_only(prov, 77)

    assert resolved is True
    assert errors == ()


def test_resolve_only_already_resolved_is_a_noop_success() -> None:
    nodes = [_inline_thread("T1", resolved=True, body="our finding", comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT":
            raise AssertionError("resolve_only must never issue a dismiss PUT")
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    resolved, errors = resolve_only(prov, 77)

    assert resolved is True
    assert errors == ()
    # Only the thread-fetch call — no resolveReviewThread mutation needed.
    graphql_calls = [c for c in rec.calls if c[1].endswith("/graphql")]
    assert len(graphql_calls) == 1


def test_resolve_only_thread_not_found() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response([]))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    resolved, errors = resolve_only(prov, 999)

    assert resolved is False
    assert any("could not locate" in e for e in errors)


def test_resolve_only_ignores_ownership_matches_bash_behavior() -> None:
    """Bash's resolve-on-success step resolves the thread containing
    PARENT_COMMENT_ID unconditionally — no marker/author gate — because the
    slash command was already validated upstream as a reply to one of our
    comments. resolve_only must not silently add an ownership check bash
    never had."""
    nodes = [_inline_thread("T1", resolved=False, body="not our marker at all", comment_db_id=77, review_db_id=None)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    resolved, errors = resolve_only(prov, 77)

    assert resolved is True
    assert errors == ()


def test_resolve_only_graphql_200_with_errors_surfaces_not_silent() -> None:
    """The #555 failure class: a GraphQL 200-with-errors body must not read
    as an empty thread list (which would look identical to 'not found')."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json={"errors": [{"message": "Could not resolve to a Repository"}]})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    resolved, errors = resolve_only(prov, 77)

    assert resolved is False
    assert any("Could not resolve to a Repository" in e for e in errors)


def test_resolve_only_resolve_mutation_failure_surfaces() -> None:
    nodes = [_inline_thread("T1", resolved=False, body="our finding", comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(403, text="Resource not accessible")
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    resolved, errors = resolve_only(prov, 77)

    assert resolved is False
    assert any("resolve thread" in e and "403" in e for e in errors)


# ---------------------------------------------------------------------------
# fetch_review_comment / context_from_parent_comment (story 13-4)
# ---------------------------------------------------------------------------


def test_context_from_parent_comment_happy_path() -> None:
    f = _finding("SQL injection", source="security-reviewer", file="db.py", line=42)
    body = _build_inline_comment_body(f, finding_id=3)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/pulls/comments/123" in str(req.url):
            return httpx.Response(200, json={"user": {"login": "github-actions[bot]"}, "path": "db.py", "body": body})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    context = context_from_parent_comment(prov, 123)

    assert context.source == "security-reviewer"
    assert context.file == "db.py"
    assert context.missing_reason == ""


def test_context_from_parent_comment_wrong_author_rejected() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/pulls/comments/123" in str(req.url):
            return httpx.Response(200, json={"user": {"login": "some-human"}, "path": "db.py", "body": "hi"})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    context = context_from_parent_comment(prov, 123)

    assert context.source == ""
    assert "not from the AI reviewer" in context.missing_reason
    assert "some-human" in context.missing_reason


def test_context_from_parent_comment_fetch_failure_surfaces() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    prov, _ = _make_provider(handler)
    context = context_from_parent_comment(prov, 123)

    assert context.source == ""
    assert "could not fetch parent comment" in context.missing_reason


def test_context_from_parent_comment_no_parent_id_short_circuits() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the API with no parent comment id")

    prov, _ = _make_provider(handler)
    context = context_from_parent_comment(prov, 0)

    assert "no parent comment" in context.missing_reason


def test_context_from_parent_comment_unparseable_header_still_sets_file() -> None:
    """Bot-authored comment fetched fine, but the header doesn't match the
    rendered format (e.g. a manually-edited comment) — matches bash's
    behavior of still exporting file= before giving up on source/rule_id."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/pulls/comments/123" in str(req.url):
            return httpx.Response(
                200, json={"user": {"login": "github-actions[bot]"}, "path": "db.py", "body": "not a rendered finding"}
            )
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    context = context_from_parent_comment(prov, 123)

    assert context.source == ""
    assert context.file == "db.py"
    assert "could not parse source tag" in context.missing_reason


# ---------------------------------------------------------------------------
# submit_approval — thin POST primitive (issue #590)
# ---------------------------------------------------------------------------


def test_submit_approval_happy_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/pulls/1/reviews"):
            body = _json.loads(req.content)
            assert body["event"] == "APPROVE"
            assert body["body"] == "all clear"
            assert "comments" not in body
            assert "commit_id" not in body
            return httpx.Response(200, json={"id": 999, "state": "APPROVED"})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    ok, status, _ = prov.submit_approval("all clear")

    assert ok is True
    assert status == 200


def test_submit_approval_http_error_surfaces() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="Validation failed")

    prov, _ = _make_provider(handler)
    ok, status, body_snippet = prov.submit_approval("all clear")

    assert ok is False
    assert status == 422
    assert "Validation failed" in body_snippet


# ---------------------------------------------------------------------------
# PR-wide approve-on-clear (issue #590)
# ---------------------------------------------------------------------------


def _cr_review(rid: int, body: str = "") -> dict:
    return {"id": rid, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": body}


@dataclass
class _StatefulReviews:
    """A small stateful review-list fixture for the approve-on-clear tests.

    Unlike the other tests in this file (which use static, unconditional mock
    responses), the approve path makes *two* separate GitHub calls that must
    agree on a review's live state -- `list_bot_reviews()` (PR-wide snapshot)
    and `get_review_state()` (per-review re-check right before dismissing).
    A dismiss PUT must be reflected in both for the subsequent call in the
    same test to see a consistent world, exactly as production GitHub would:
    a review the approve path just dismissed is DISMISSED, not
    CHANGES_REQUESTED, the next time anything asks.
    """

    reviews: dict[int, dict]

    def state(self, rid: int) -> str | None:
        r = self.reviews.get(rid)
        return r["state"] if r else None

    def dismiss(self, rid: int) -> None:
        if rid in self.reviews:
            self.reviews[rid]["state"] = "DISMISSED"

    def as_list(self) -> list[dict]:
        return list(self.reviews.values())


def test_dismiss_inline_reply_approves_when_last_review_cleared() -> None:
    """The defining #590 happy path: a single outstanding CHANGES_REQUESTED
    review, its last unresolved thread is resolved by this call, and the
    actor is trusted (approve_allowed=True) -- the review is dismissed and a
    fresh APPROVE review is submitted.

    This also pins the ordering fix that makes #590 reachable at all: the
    PR-wide approve check must run BEFORE the per-review dismiss fallback, or
    the per-review dismiss would flip review 41 to DISMISSED first and the
    approve path's subsequent list_bot_reviews() would see zero
    CHANGES_REQUESTED reviews -- silently never approving even in this
    single-review case. Caught via a stateful mock (a naive always-CR mock
    hides this ordering bug entirely -- see PR discussion)."""
    nodes = [_inline_thread("T1", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41)]
    state = _StatefulReviews({41: _cr_review(41)})
    approved: list[dict] = []
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            s = state.state(41)
            return httpx.Response(200, json={"id": 41, "state": s})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            dismissed.append(url)
            state.dismiss(41)
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            approved.append(_json.loads(req.content))
            return httpx.Response(200, json={"id": 999, "state": "APPROVED"})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss", approve_allowed=True)

    assert result.thread_resolved is True
    assert result.review_dismissed is True
    assert result.pr_approved is True
    assert len(approved) == 1
    assert approved[0]["event"] == "APPROVE"
    # Exactly one dismissal PUT for review 41 -- not two. The pre-fix ordering
    # (per-review dismiss running before the PR-wide approve check) caused a
    # second, redundant PUT here when tested against a mock naive enough not
    # to notice; this stateful mock would surface that as a second entry.
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]
    assert "approved" in result.reply
    assert result.errors == ()


def test_approve_partial_failure_surfaces_dismissed_ids_when_submit_approval_fails() -> None:
    """A transient submit_approval failure (e.g. HTTP 5xx) after review 41 has
    already been dismissed via a real, non-retractable PUT must not be
    silent: .errors names the already-dismissed review id so the caller's
    reply/log can flag the inconsistent state (dismissed but not approved)
    rather than reporting only the submit_approval failure in isolation."""
    nodes = [_inline_thread("T1", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41)]
    state = _StatefulReviews({41: _cr_review(41)})
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": state.state(41)})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            dismissed.append(url)
            state.dismiss(41)
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            return httpx.Response(500, text="internal error")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss", approve_allowed=True)

    assert result.thread_resolved is True
    assert result.pr_approved is False
    # Review 41 really was dismissed (the PUT above landed) before the
    # approval call failed -- the per-review fallback must not re-report this
    # as "nothing happened": get_review_state(41) reads back non-CR, so
    # _dismiss_if_all_resolved's own guard treats it as a no-op.
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]
    assert result.review_dismissed is False
    assert any("submit_approval" in e and "41" in e for e in result.errors)


def test_dismiss_inline_reply_no_approve_when_other_cr_review_still_open() -> None:
    """PR-wide scoping (issue #590's core design decision): clearing every
    thread on ONE of several CHANGES_REQUESTED reviews must NOT trigger an
    approve while another bot CHANGES_REQUESTED review still has unresolved
    findings. Only the one cleared review is dismissed; no APPROVE POST."""
    # Review 41 is the one being cleared by this call; review 42 is a second,
    # still-open CHANGES_REQUESTED review with its own unresolved thread.
    nodes = [
        _inline_thread("T1", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41),
        _inline_thread("T2", resolved=False, body=f"y\n{INLINE_MARKER}", comment_db_id=88, review_db_id=42),
    ]
    state = _StatefulReviews({41: _cr_review(41), 42: _cr_review(42)})
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": state.state(41)})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            dismissed.append(url)
            state.dismiss(41)
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError("must not approve while another CR review still has unresolved findings")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss", approve_allowed=True)

    assert result.thread_resolved is True
    assert result.review_dismissed is True  # review 41 alone is fully resolved
    assert result.pr_approved is False
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]
    assert "approved" not in result.reply
    assert result.errors == ()


def test_dismiss_inline_reply_no_approve_when_not_allowed() -> None:
    """Trust gate: even when clearing the last active finding PR-wide,
    approve_allowed=False (the default, and what a COLLABORATOR-level actor
    gets per the workflow's tighter OWNER/MEMBER bar for auto-approve) must
    skip the approve step entirely -- no extra list_bot_reviews call, no
    APPROVE POST -- while the ordinary dismiss still proceeds via the
    existing per-review path."""
    nodes = [_inline_thread("T1", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41)]
    state = _StatefulReviews({41: _cr_review(41)})
    list_bot_reviews_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal list_bot_reviews_calls
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": state.state(41)})
        if req.method == "GET" and "/reviews" in url:
            list_bot_reviews_calls += 1
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            state.dismiss(41)
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError("must not approve when approve_allowed=False")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss", approve_allowed=False)

    assert result.thread_resolved is True
    assert result.review_dismissed is True
    assert result.pr_approved is False
    # approve_allowed=False short-circuits _approve_if_pr_fully_resolved
    # before any list_bot_reviews call -- the dismiss above went through the
    # existing per-review _dismiss_if_all_resolved path instead.
    assert list_bot_reviews_calls == 0
    assert "approved" not in result.reply


def test_dismiss_by_finding_id_approves_when_last_review_cleared() -> None:
    """Same PR-wide approve happy path, but via the top-level-comment /
    body-finding-ID entry point (dismiss_by_finding_id) rather than the
    inline-reply entry point, proving both call sites share the same
    approve wiring and ordering fix."""
    id_map = {"security-reviewer|api.py|10|abc123456789": 4}
    our_body = f"[High] leak\n**[F4]**\n{INLINE_MARKER}\n" + build_id_map_marker(id_map)
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    state = _StatefulReviews({41: _cr_review(41, body=our_body)})
    approved: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": state.state(41)})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            state.dismiss(41)
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            approved.append(_json.loads(req.content))
            return httpx.Response(200, json={"id": 999, "state": "APPROVED"})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="false-positive", approve_allowed=True)

    assert result.thread_resolved is True
    assert result.review_dismissed is True
    assert result.pr_approved is True
    assert len(approved) == 1
    assert "approved" in result.reply


def test_approve_race_guard_review_flips_state_before_dismiss() -> None:
    """Race safety (mirrors _dismiss_if_all_resolved's get_review_state
    re-check): the review is already DISMISSED by a concurrent run by the
    time list_bot_reviews() is fetched, so the PR-wide approve check finds no
    CHANGES_REQUESTED reviews at all and cleanly declines -- proving the
    approve path never manufactures a dismiss/approve action from stale
    state. The per-review fallback dismiss then also independently confirms
    the same DISMISSED state via its own get_review_state re-check."""
    nodes = [_inline_thread("T1", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41)]
    # Review 41 already flipped to DISMISSED before this call even starts --
    # simulates a concurrent run winning the race.
    state = _StatefulReviews({41: {**_cr_review(41), "state": "DISMISSED"}})

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": state.state(41)})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            raise AssertionError("must not dismiss a review already flipped away from CHANGES_REQUESTED")
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError("must not approve when no CHANGES_REQUESTED review remains")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss", approve_allowed=True)

    assert result.thread_resolved is True
    assert result.review_dismissed is False
    assert result.pr_approved is False
    assert result.errors == ()


def test_approve_race_guard_state_fetch_failure_fails_closed() -> None:
    """A get_review_state HTTP failure during the PR-wide approve path's
    per-review re-check must fail closed: no approve, no dismiss PUT, and the
    failure surfaces in .errors -- never silently proceed on unverifiable
    state. Because the approve path now runs FIRST (before the per-review
    fallback), this failure also means the per-review fallback never gets a
    chance to dismiss review 41 either -- the whole call reports nothing
    dismissed, matching _dismiss_if_all_resolved's own fail-closed contract."""
    nodes = [_inline_thread("T1", resolved=False, body=f"x\n{INLINE_MARKER}", comment_db_id=77, review_db_id=41)]
    state = _StatefulReviews({41: _cr_review(41)})

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(500, text="internal error")
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=state.as_list())
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            raise AssertionError("must not dismiss when review state could not be verified")
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError("must not approve when review state could not be verified")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="dismiss", approve_allowed=True)

    assert result.thread_resolved is True
    assert result.review_dismissed is False
    assert result.pr_approved is False
    assert any("get_review_state" in e for e in result.errors)
