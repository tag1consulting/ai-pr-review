"""Structural Protocol conformance tests for VcsProvider implementations."""

from __future__ import annotations

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.protocol import VcsProvider


def test_github_provider_satisfies_vcs_provider_protocol() -> None:
    import httpx

    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=[]))
    http = httpx.Client(transport=transport, base_url="https://api.github.com")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    provider = GitHubProvider(config=config, client=client)
    assert isinstance(provider, VcsProvider)
