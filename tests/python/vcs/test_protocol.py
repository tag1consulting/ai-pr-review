"""Structural Protocol conformance tests for VcsProvider implementations."""

from __future__ import annotations

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.protocol import StaleResult, SummaryResult, VcsProvider


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


def test_summary_result_ok_requires_comment_id_when_created_or_updated() -> None:
    """SummaryResult.ok is False when comment_id is None but created/updated is True."""
    # Success: comment_id present
    assert SummaryResult(comment_id=42, created=True, updated=False).ok is True
    assert SummaryResult(comment_id=42, created=False, updated=True).ok is True
    # No-op path (nothing posted, no error): ok
    assert SummaryResult(comment_id=None, created=False, updated=False).ok is True
    # Error path: not ok regardless of comment_id
    assert SummaryResult(comment_id=42, created=True, updated=False, error="boom").ok is False
    assert SummaryResult(comment_id=None, created=False, updated=False, error="boom").ok is False
    # Broken state: created=True but comment_id=None (API returned id=0): not ok
    assert SummaryResult(comment_id=None, created=True, updated=False).ok is False


def test_stale_result_errors_is_tuple() -> None:
    """B7/A4: StaleResult.errors must be a tuple, not a mutable list."""
    result = StaleResult(errors=("one", "two"))
    assert isinstance(result.errors, tuple)
    # Default is also a tuple (empty)
    default = StaleResult()
    assert isinstance(default.errors, tuple)
    assert default.errors == ()
    assert default.threads_resolved == 0
    assert default.reviews_dismissed == 0
    assert default.threads_skipped_no_marker == 0
