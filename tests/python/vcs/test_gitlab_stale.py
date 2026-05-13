"""GitLabProvider.resolve_stale — marker-gated discussion resolution."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.vcs.gitlab import GitLabConfig, GitLabProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER
from ai_pr_review.vcs.protocol import VcsProvider


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GitLabProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://gitlab.com/api/v4")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    return GitLabProvider(
        config=GitLabConfig(
            project_id_or_path="42",
            mr_iid=1,
            token="glpat-test",
            diff_base_sha="basesha",
            bot_username="ai-bot",
        ),
        client=client,
    )


def _disc(
    did: str,
    *,
    body: str,
    author: str,
    resolvable: bool = True,
    resolved: bool = False,
) -> dict:
    return {
        "id": did,
        "notes": [
            {
                "body": body,
                "author": {"username": author},
                "resolvable": resolvable,
                "resolved": resolved,
            }
        ],
    }


def test_resolve_stale_skips_other_bot_discussions() -> None:
    """Closes #184 (GitLab half): other bots' discussions must not be touched."""
    discussions = [
        # Renovate's discussion — no marker, different author. MUST be untouched.
        _disc("D_dep", body="dependency update", author="renovate-bot"),
        _disc("D_human", body="reviewer feedback", author="alice"),
    ]
    resolves: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=discussions)
        if req.method == "PUT" and "/discussions/" in str(req.url):
            resolves.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 2
    assert resolves == []


def test_resolve_stale_resolves_our_markered_discussions() -> None:
    discussions = [
        _disc("D_ours", body=f"finding\n{INLINE_MARKER}", author="ai-bot"),
    ]
    resolves: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=discussions)
        if req.method == "PUT" and "/discussions/" in str(req.url):
            resolves.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 1
    assert len(resolves) == 1
    assert "/discussions/D_ours" in resolves[0]
    assert "resolved=true" in resolves[0]


def test_resolve_stale_marker_but_wrong_author_skipped() -> None:
    discussions = [
        _disc("D_spoof", body=f"x\n{INLINE_MARKER}", author="evil-bot"),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=discussions)
        if req.method == "PUT":
            raise AssertionError("Should not call PUT on spoofed discussion")
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 1


def test_resolve_stale_already_resolved_silent() -> None:
    discussions = [
        _disc(
            "D_done",
            body=f"x\n{INLINE_MARKER}",
            author="ai-bot",
            resolved=True,
        ),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=discussions)
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0
    assert result.threads_skipped_no_marker == 0


def test_resolve_stale_unresolvable_skipped() -> None:
    discussions = [
        _disc(
            "D_meta",
            body=f"x\n{INLINE_MARKER}",
            author="ai-bot",
            resolvable=False,
        ),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=discussions)
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.resolve_stale()
    assert result.threads_resolved == 0


def test_resolve_stale_fetches_bot_username_when_unset() -> None:
    """When config.bot_username is None, provider calls /user once and caches."""
    user_calls = {"n": 0}
    discussions = [_disc("D_x", body=f"x\n{INLINE_MARKER}", author="dynamic-bot")]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and str(req.url).endswith("/user"):
            user_calls["n"] += 1
            return httpx.Response(200, json={"username": "dynamic-bot"})
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=discussions)
        if req.method == "PUT" and "/discussions/" in str(req.url):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://gitlab.com/api/v4")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    prov = GitLabProvider(
        config=GitLabConfig(
            project_id_or_path="42",
            mr_iid=1,
            token="glpat-test",
            diff_base_sha="basesha",
            bot_username=None,
        ),
        client=client,
    )
    result = prov.resolve_stale()
    assert result.threads_resolved == 1
    assert user_calls["n"] == 1


def test_gitlab_provider_satisfies_protocol() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=[]))
    http = httpx.Client(transport=transport, base_url="https://gitlab.com/api/v4")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    prov = GitLabProvider(
        config=GitLabConfig(
            project_id_or_path="1",
            mr_iid=1,
            token="t",
            diff_base_sha="abc",
        ),
        client=client,
    )
    assert isinstance(prov, VcsProvider)
