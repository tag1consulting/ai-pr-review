"""GitHubProvider.resolve_stale — marker-gated thread resolution (closes #183, #184)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[GitHubProvider, _Recorder]:
    rec = _Recorder()

    def _wrap(request: httpx.Request) -> httpx.Response:
        import json

        body = None
        if request.content:
            try:
                body = json.loads(request.content)
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


def _thread(
    tid: str,
    *,
    resolved: bool,
    body: str,
    author: str,
    review_db_id: int | None = None,
) -> dict:
    inner: dict = {"body": body, "author": {"login": author}}
    if review_db_id is not None:
        inner["pullRequestReview"] = {"databaseId": review_db_id}
    else:
        inner["pullRequestReview"] = None
    return {
        "id": tid,
        "isResolved": resolved,
        "comments": {"nodes": [inner]},
    }


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


def test_resolve_stale_skips_threads_without_marker() -> None:
    """Critical #183: threads from OTHER bots must NOT be resolved."""
    nodes = [
        # Dependabot's review thread — no marker. MUST be left alone.
        _thread("T_dep", resolved=False, body="## Dependabot alert", author="dependabot[bot]"),
        # Renovate's thread — no marker. MUST be left alone.
        _thread("T_ren", resolved=False, body="This dependency update…", author="renovate[bot]"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 2
    # No resolveReviewThread mutation should have been issued.
    graphql_bodies = [c[2] for c in rec.calls if c[2] and "query" in c[2]]
    assert not any("resolveReviewThread" in (b.get("query") or "") for b in graphql_bodies)


def test_resolve_stale_resolves_our_markered_threads() -> None:
    our_body = f"[High] bad\n{INLINE_MARKER}"
    nodes = [
        _thread("T_ours", resolved=False, body=our_body, author="github-actions[bot]"),
    ]
    mutations: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                mutations.append(body["variables"]["id"])
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T_ours", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 1
    assert result.threads_skipped_no_marker == 0
    assert mutations == ["T_ours"]


def test_resolve_stale_marker_but_different_author_skipped() -> None:
    """Defense in depth: marker present but author differs → skip."""
    # Imagine a malicious PR body that copies our marker into its own comment
    spoofed_body = f"fake finding\n{INLINE_MARKER}"
    nodes = [
        _thread("T_spoof", resolved=False, body=spoofed_body, author="other-bot[bot]"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 1


def test_resolve_stale_already_resolved_skipped_silently() -> None:
    nodes = [
        _thread("T_done", resolved=True, body=f"x\n{INLINE_MARKER}", author="github-actions[bot]"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 0


def test_resolve_stale_dismisses_only_our_changes_requested_with_all_threads_resolved() -> None:
    """Dismiss our CHANGES_REQUESTED review only after all of OUR threads are resolved."""
    # Our review #42 had one markered thread, now resolved
    nodes = [
        _thread(
            "T42",
            resolved=True,
            body=f"old finding\n{INLINE_MARKER}",
            author="github-actions[bot]",
            review_db_id=42,
        ),
    ]
    reviews = [
        {"id": 42, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}},
        # someone else's review — must NOT be dismissed
        {"id": 43, "state": "CHANGES_REQUESTED", "user": {"login": "other-bot[bot]"}},
    ]
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=reviews)
        if req.method == "PUT" and "/dismissals" in str(req.url):
            dismissed.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.reviews_dismissed == 1
    assert len(dismissed) == 1
    assert "/reviews/42/dismissals" in dismissed[0]


def test_resolve_stale_wont_dismiss_when_unresolved_markered_thread_exists() -> None:
    nodes = [
        _thread(
            "T42",
            resolved=False,
            body=f"still bad\n{INLINE_MARKER}",
            author="github-actions[bot]",
            review_db_id=42,
        ),
    ]
    reviews = [
        {"id": 42, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T42", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=reviews)
        if req.method == "PUT":
            # should never be called
            raise AssertionError("PUT dismiss should not be called")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    # Thread was resolved in THIS call; but the dismiss path looks at the
    # pre-fetched thread list and sees the thread was unresolved at fetch time.
    # So we still hold off on dismissing this run — the NEXT run will dismiss it.
    assert result.threads_resolved == 1
    assert result.reviews_dismissed == 0


def test_resolve_thread_failure_includes_http_status_in_error() -> None:
    """B4: When _resolve_thread fails, the error string must include the HTTP status."""
    import json as _json

    nodes = [
        _thread("T99", resolved=False, body=f"bad\n{INLINE_MARKER}", author="github-actions[bot]"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(403, json={"message": "forbidden"})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert len(result.errors) == 1
    assert "403" in result.errors[0]
    assert "T99" in result.errors[0]


def test_fetch_review_threads_graphql_200_with_errors_recorded() -> None:
    """GraphQL 200-with-errors must be captured in StaleResult.errors, not silently ignored."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(
                200,
                json={"errors": [{"message": "Not authorized to read review threads"}]},
            )
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert any("Not authorized to read review threads" in e for e in result.errors)


def test_dismiss_stale_reviews_paginates_reviews_list() -> None:
    """_dismiss_stale_reviews must follow Link: rel=next headers when listing reviews."""
    # All threads are resolved so dismiss logic proceeds
    nodes: list[dict] = []

    # Page 1: no matching reviews
    reviews_page1 = [
        {"id": 10, "state": "APPROVED", "user": {"login": "github-actions[bot]"}},
    ]
    # Page 2: the CHANGES_REQUESTED review we should dismiss
    reviews_page2 = [
        {"id": 42, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}},
    ]
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        url_str = str(req.url)
        if req.method == "GET" and "/reviews" in url_str:
            if "page=2" in url_str:
                return httpx.Response(200, json=reviews_page2)
            # First page: return with a Link: next header pointing to page 2
            base = "https://api.github.com/repos/o/r/pulls/1/reviews"
            link = f'<{base}?page=2>; rel="next"'
            return httpx.Response(200, json=reviews_page1, headers={"link": link})
        if req.method == "PUT" and "/dismissals" in url_str:
            dismissed.append(url_str)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.reviews_dismissed == 1
    assert any("/reviews/42/dismissals" in d for d in dismissed)
