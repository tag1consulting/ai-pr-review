"""BitbucketProvider.post_findings — combined comment model."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, SUMMARY_MARKER_PREFIX
from ai_pr_review.vcs.protocol import DiffContext


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> BitbucketProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    return BitbucketProvider(
        config=BitbucketConfig(
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t"
        ),
        client=client,
    )


_HEAD = "abc1234def5678abc1234def5678abc1234def56"


def _existing_summary(comment_id: int = 100, sha: str = "01d4ead4ee") -> dict:
    return {
        "id": comment_id,
        "content": {
            "raw": (
                f"<!-- ai-pr-review-summary sha={sha} -->\n"
                "## AI Review: Approved\n\n"
                "No findings yet."
            )
        },
    }


def test_post_findings_no_existing_returns_error() -> None:
    """Bitbucket requires post_summary to run first (AC5 ordering)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": []})

    prov = _make_provider(handler)
    findings = [Finding(severity="High", confidence=90, finding="x")]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert not result.ok
    assert "no summary comment" in (result.error or "")
    assert result.body_findings == 1


def test_post_findings_appends_into_existing_comment() -> None:
    captured: list[dict] = []
    existing = _existing_summary(comment_id=42)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="SQLi via string concat",
            source="blind",
            file="db.py",
            line=12,
            remediation="parameterize",
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok
    assert result.body_findings == 1
    assert result.review_id == 42

    raw = captured[0]["content"]["raw"]
    assert SUMMARY_MARKER_PREFIX in raw
    assert "## AI Review Findings" in raw
    assert "**Overall Risk:** High" in raw
    assert "SQLi via string concat" in raw
    assert "parameterize" in raw
    # Inline marker tagged on the body too
    assert INLINE_MARKER in raw


def test_post_findings_approve_with_no_findings_renders_approved_block() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [_existing_summary()]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 100})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [], DiffContext(diff_text="", head_sha=_HEAD), event="APPROVE"
    )
    assert result.ok
    raw = captured[0]["content"]["raw"]
    assert "AI Review: Approved" in raw


def test_post_findings_incomplete_when_failed_agents_and_no_findings() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [_existing_summary()]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 100})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [],
        DiffContext(diff_text="", head_sha=_HEAD),
        event="COMMENT",
        failed_agents=["blind-hunter"],
    )
    assert result.ok
    raw = captured[0]["content"]["raw"]
    assert "AI Review: Incomplete" in raw
    assert "blind-hunter" in raw


def test_post_findings_put_failure_returns_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [_existing_summary()]})
        if req.method == "PUT":
            return httpx.Response(422, text="invalid")
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [Finding(severity="Low", confidence=50, finding="x")]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="COMMENT"
    )
    assert not result.ok
    assert "HTTP 422" in (result.error or "")
