"""GitHubProvider's elevated-token client split (#734).

`resolve_thread`/`unresolve_thread` are the only two GitHub API calls this
provider makes that require a PAT/App token (GitHub blocks the
`resolveReviewThread` GraphQL mutation under the default Actions token from
a comment-triggered workflow). Every other write must stay on the regular
`client` so it's attributed to that token's identity instead of the
elevated one -- see `docs/slash-commands.md#pat-requirement`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


@dataclass
class _Recorder:
    calls: list[tuple[str, str]] = field(default_factory=list)


def _client(name: str, rec: _Recorder, handler: Callable[[httpx.Request], httpx.Response]) -> RecordingClient:
    def _wrap(request: httpx.Request) -> httpx.Response:
        rec.calls.append((name, request.method))
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(_wrap), base_url="https://api.github.com")
    return RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )


def _ok_graphql(name: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {f"{name}ReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
        )

    return handler


def _make_split_provider(rec: _Recorder) -> GitHubProvider:
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="bot-token", elevated_token="pat-token")
    client = _client("safe", rec, lambda _req: httpx.Response(200, json={"id": 1}))
    elevated_client = _client("elevated", rec, _ok_graphql("resolve"))
    return GitHubProvider(config=config, client=client, elevated_client=elevated_client)


def test_resolve_thread_uses_elevated_client_when_configured() -> None:
    rec = _Recorder()
    prov = _make_split_provider(rec)
    ok, status, _ = prov.resolve_thread("T1")
    assert ok is True
    assert status == 200
    assert rec.calls == [("elevated", "POST")]


def test_unresolve_thread_uses_elevated_client_when_configured() -> None:
    rec = _Recorder()
    prov = _make_split_provider(rec)
    ok, status, _ = prov.unresolve_thread("T1")
    assert ok is True
    assert status == 200
    assert rec.calls == [("elevated", "POST")]


def test_dismiss_review_never_uses_elevated_client() -> None:
    """The write that dismisses a superseded review must stay on the safe
    (bot-attributed) client even when an elevated client is configured --
    only resolve_thread/unresolve_thread are exempt."""
    rec = _Recorder()
    prov = _make_split_provider(rec)
    ok, _status, _ = prov.dismiss_review(99, "superseded")
    assert ok is True
    assert rec.calls == [("safe", "PUT")]


def test_reply_to_review_comment_never_uses_elevated_client() -> None:
    rec = _Recorder()
    prov = _make_split_provider(rec)
    ok, _status, _ = prov.reply_to_review_comment(99, "hello")
    assert ok is True
    assert rec.calls == [("safe", "POST")]


def test_no_elevated_client_falls_back_to_regular_client() -> None:
    """When no elevated token is configured (single-token consumers), the
    thread-resolution mutations route through the same client as
    everything else -- pre-#734 behavior, unchanged."""
    rec = _Recorder()
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="only-token")
    client = _client("only", rec, _ok_graphql("resolve"))
    prov = GitHubProvider(config=config, client=client)
    ok, _status, _ = prov.resolve_thread("T1")
    assert ok is True
    assert rec.calls == [("only", "POST")]
