"""GitHubProvider primitives for canonical-review reuse: list_bot_reviews
(already existed) plus update_review_body (new)."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GitHubProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.github.com")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    return GitHubProvider(config=config, client=client)


def test_update_review_body_puts_new_body() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        if req.method == "PUT" and "/reviews/42" in str(req.url):
            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 42, "body": "new body"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    ok, status, _ = prov.update_review_body(42, "new body")

    assert ok
    assert status == 200
    assert captured == [{"body": "new body"}]


def test_update_review_body_never_sends_event_or_state() -> None:
    """PUT /reviews/{id} can only ever change body -- confirmed against
    GitHub's own REST docs. The request payload must never include an
    `event` or `state` key; a caller trying to sneak a state change through
    this primitive would silently no-op on GitHub's side, not error, so the
    payload shape itself is the only place to catch that mistake."""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        if req.method == "PUT" and "/reviews/7" in str(req.url):
            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(404)

    prov = _make_provider(handler)
    prov.update_review_body(7, "some body")

    assert captured == [{"body": "some body"}]
    assert "event" not in captured[0]
    assert "state" not in captured[0]


def test_update_review_body_failure_returns_error_details() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="Validation Failed")

    prov = _make_provider(handler)
    ok, status, body_snippet = prov.update_review_body(42, "new body")

    assert not ok
    assert status == 422
    assert "Validation Failed" in body_snippet


def test_update_review_body_uses_the_review_url_not_dismiss_url() -> None:
    """Must PUT /pulls/{n}/reviews/{id}, not the dismissals sub-resource
    (/pulls/{n}/reviews/{id}/dismissals) that dismiss_review uses -- easy
    to conflate since both are PUTs to a review-scoped URL."""
    urls_hit: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls_hit.append(str(req.url))
        return httpx.Response(200, json={"id": 42})

    prov = _make_provider(handler)
    prov.update_review_body(42, "body")

    assert len(urls_hit) == 1
    assert urls_hit[0].endswith("/pulls/1/reviews/42")
    assert "dismissals" not in urls_hit[0]
