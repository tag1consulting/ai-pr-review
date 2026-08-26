"""GitHubProvider tests: post_check_run (the ai-pr-review/policy-gate merge gate)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


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
                import json

                body = json.loads(request.content)
            except Exception:
                body = {"_raw": request.content.decode("utf-8", errors="replace")}
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
    config = GitHubConfig(owner="o", repo="r", pr_number=7, token="t")
    return GitHubProvider(config=config, client=client), rec


def test_post_check_run_success_posts_correct_payload() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    provider, rec = _make_provider(handler)
    ok = provider.post_check_run(
        head_sha="abc1234",
        name="ai-pr-review/policy-gate",
        conclusion="success",
        title="'deep' review tier satisfied",
        summary="This run satisfies the 'deep' review tier.",
    )
    assert ok is True
    assert len(rec.calls) == 1
    method, url, body = rec.calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/o/r/check-runs"
    assert body == {
        "name": "ai-pr-review/policy-gate",
        "head_sha": "abc1234",
        "status": "completed",
        "conclusion": "success",
        "output": {
            "title": "'deep' review tier satisfied",
            "summary": "This run satisfies the 'deep' review tier.",
        },
    }


def test_post_check_run_neutral_conclusion() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    provider, rec = _make_provider(handler)
    provider.post_check_run(
        head_sha="abc1234",
        name="ai-pr-review/policy-gate",
        conclusion="neutral",
        title="'deep' review tier required",
        summary="Comment /ai-pr-review review-full to satisfy it.",
    )
    _, _, body = rec.calls[0]
    assert body is not None
    assert body["conclusion"] == "neutral"


def test_post_check_run_api_error_returns_false_and_records_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Resource not accessible by integration")

    provider, _rec = _make_provider(handler)
    ok = provider.post_check_run(
        head_sha="abc1234",
        name="ai-pr-review/policy-gate",
        conclusion="success",
        title="t",
        summary="s",
    )
    assert ok is False
    assert any("post_check_run" in e for e in provider._errors)
