"""Bitbucket parity (#918): the decided review outcome (APPROVE/
REQUEST_CHANGES/COMMENT) must have a real effect on the PR's own reviewer
state via Bitbucket's approve/request-changes endpoints, not just render as
heading text in the comment body.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import SUMMARY_MARKER_PREFIX
from ai_pr_review.vcs.protocol import DiffContext

_HEAD = "abc1234def5678abc1234def5678abc1234def56"

_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 context_line_1
 context_line_2
 context_line_3
+added_line_4
"""

_TEST_BOT_ACCOUNT_ID = "test-bot-account-id"


def _existing_summary(comment_id: int = 100) -> dict:
    return {
        "id": comment_id,
        "content": {
            "raw": f"{SUMMARY_MARKER_PREFIX} sha={_HEAD} -->\n## AI Review: Approved\n\nNo findings yet.",
        },
        "user": {"account_id": _TEST_BOT_ACCOUNT_ID},
    }


def _make_provider(
    calls: list[tuple[str, str]],
    *,
    review_state: bool = True,
    on_approve: Callable[[httpx.Request], httpx.Response] | None = None,
    on_request_changes: Callable[[httpx.Request], httpx.Response] | None = None,
) -> BitbucketProvider:
    existing = _existing_summary()

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((req.method, url))
        if req.method == "GET" and url.rstrip("/").endswith("/user"):
            return httpx.Response(200, json={"account_id": _TEST_BOT_ACCOUNT_ID})
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            return httpx.Response(200, json={"id": existing["id"]})
        if url.endswith("/approve"):
            if req.method == "DELETE":
                return httpx.Response(404)
            if req.method == "POST":
                if on_approve is not None:
                    return on_approve(req)
                return httpx.Response(200, json={})
        if url.endswith("/request-changes"):
            if req.method == "DELETE":
                return httpx.Response(404)
            if req.method == "POST":
                if on_request_changes is not None:
                    return on_request_changes(req)
                return httpx.Response(200, json={})
        return httpx.Response(404)

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
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t",
            code_insights=False, review_state=review_state,
        ),
        client=client,
    )


def _non_get(calls: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [c for c in calls if c[0] != "GET"]


def test_approve_event_deletes_request_changes_then_posts_approve() -> None:
    calls: list[tuple[str, str]] = []
    prov = _make_provider(calls)
    finding = Finding(severity="Low", confidence=70, finding="nit", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="APPROVE"
    )
    assert result.ok, result.error
    state_calls = [c for c in _non_get(calls) if c[1].endswith(("/approve", "/request-changes"))]
    assert [m for m, u in state_calls] == ["DELETE", "POST"]
    assert state_calls[0][1].endswith("/request-changes")
    assert state_calls[1][1].endswith("/approve")


def test_request_changes_event_deletes_approve_then_posts_request_changes() -> None:
    calls: list[tuple[str, str]] = []
    prov = _make_provider(calls)
    finding = Finding(severity="Critical", confidence=90, finding="sql injection", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    state_calls = [c for c in _non_get(calls) if c[1].endswith(("/approve", "/request-changes"))]
    assert [m for m, u in state_calls] == ["DELETE", "POST"]
    assert state_calls[0][1].endswith("/approve")
    assert state_calls[1][1].endswith("/request-changes")


def test_comment_event_clears_both_prior_states() -> None:
    calls: list[tuple[str, str]] = []
    prov = _make_provider(calls)
    finding = Finding(severity="Low", confidence=70, finding="nit", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    state_calls = [c for c in _non_get(calls) if c[1].endswith(("/approve", "/request-changes"))]
    assert [m for m, u in state_calls] == ["DELETE", "DELETE"]
    assert {u.rsplit("/", 1)[-1] for _m, u in state_calls} == {"approve", "request-changes"}


def test_404_on_delete_is_not_logged_as_an_error() -> None:
    calls: list[tuple[str, str]] = []
    prov = _make_provider(calls)
    finding = Finding(severity="Critical", confidence=90, finding="sql injection", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert not any("_set_review_state DELETE" in e for e in prov._errors)


def test_post_failure_is_fail_soft_and_logged() -> None:
    calls: list[tuple[str, str]] = []

    def on_request_changes(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    prov = _make_provider(calls, on_request_changes=on_request_changes)
    finding = Finding(severity="Critical", confidence=90, finding="sql injection", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert any("_set_review_state POST" in e for e in prov._errors)


def test_post_calls_carry_a_json_body() -> None:
    """Live-verified against the real Bitbucket API (2026-09-24): a POST to
    /approve or /request-changes with this client's default
    Content-Type: application/json header but an EMPTY body gets a bare-text
    400 Bad Request, not a JSON error. Sending an explicit `{}` body fixes it
    -- the endpoints ignore body content entirely, but the Content-Type
    header has to agree with something actually being there."""
    bodies: list[bytes] = []

    def capture(req: httpx.Request) -> httpx.Response:
        bodies.append(req.content)
        return httpx.Response(200, json={})

    calls: list[tuple[str, str]] = []
    prov = _make_provider(calls, on_approve=capture, on_request_changes=capture)
    finding = Finding(severity="Critical", confidence=90, finding="sql injection", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert bodies == [b"{}"]


def test_kill_switch_makes_no_review_state_call() -> None:
    calls: list[tuple[str, str]] = []
    prov = _make_provider(calls, review_state=False)
    finding = Finding(severity="Critical", confidence=90, finding="sql injection", file="app.py", line=4)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert not any(u.endswith(("/approve", "/request-changes")) for _m, u in _non_get(calls))
