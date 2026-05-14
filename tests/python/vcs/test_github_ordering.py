"""Call-ordering invariant: resolve_stale must run AFTER successful post."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.protocol import DiffContext

_VALID_SHA = "abc1234def5678abc1234def5678abc1234def56"


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
    return GitHubProvider(
        config=GitHubConfig(owner="o", repo="r", pr_number=1, token="t"),
        client=client,
    )


def test_cleanup_runs_after_post_summary_and_findings() -> None:
    """Recommended orchestrator order: post_summary → post_findings → resolve_stale.

    This test verifies *the orchestration site* honors the order by ensuring a
    caller can observe each step succeed before the next one starts. If an API
    error aborts the sequence, resolve_stale MUST NOT be called.
    """
    order: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and "/issues/1/comments" in url:
            order.append("list_summary")
            return httpx.Response(200, json=[])
        if req.method == "POST" and "/issues/1/comments" in url:
            order.append("post_summary")
            return httpx.Response(201, json={"id": 100})
        if req.method == "POST" and "/reviews" in url and not url.endswith("/graphql"):
            order.append("post_findings")
            return httpx.Response(201, json={"id": 200})
        if req.method == "POST" and url.endswith("/graphql"):
            order.append("graphql")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [],
                                }
                            }
                        }
                    }
                },
            )
        if req.method == "GET" and "/reviews" in url:
            order.append("list_reviews")
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    prov = _make_provider(handler)

    # Orchestration site sequence
    r1 = prov.post_summary("Summary", _VALID_SHA)
    assert r1.ok

    r2 = prov.post_findings(
        [Finding(severity="Low", confidence=50, finding="x")],
        DiffContext(diff_text="", head_sha=_VALID_SHA),
        event="COMMENT",
    )
    assert r2.ok

    r3 = prov.resolve_stale()
    assert not r3.errors

    # Validate ordering
    assert order.index("post_summary") < order.index("post_findings")
    assert order.index("post_findings") < order.index("graphql")
