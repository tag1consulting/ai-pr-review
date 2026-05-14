"""GitLabProvider.post_findings — discussions, suggestions, position object."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.gitlab import GitLabConfig, GitLabProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER
from ai_pr_review.vcs.protocol import DiffContext


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
            diff_base_sha="basesha1234",
            bot_username="ai-bot",
        ),
        client=client,
    )


_HEAD = "abc1234def5678abc1234def5678abc1234def56"
_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
 ctx_1
 ctx_2
 ctx_3
+added_4
+added_5
+added_6
"""


def test_post_findings_inline_for_eligible_line() -> None:
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="unsafe call",
            source="blind",
            file="app.py",
            line=4,
            remediation="sanitize",
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.inline_posted == 1
    assert result.body_findings == 0
    assert len(posts) == 1
    pos = posts[0]["position"]
    assert pos["new_path"] == "app.py"
    assert pos["new_line"] == 4
    assert pos["base_sha"] == "basesha1234"
    assert pos["head_sha"] == _HEAD
    assert pos["position_type"] == "text"
    body = posts[0]["body"]
    assert INLINE_MARKER in body
    assert "unsafe call" in body
    assert "sanitize" in body


def test_post_findings_ineligible_line_not_inline() -> None:
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/discussions" in str(req.url):
            import json

            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(severity="Low", confidence=50, finding="x", file="app.py", line=99)
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.inline_posted == 0
    assert result.body_findings == 1
    assert posts == []


def test_post_findings_renders_multi_line_suggestion_fence() -> None:
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/discussions" in str(req.url):
            import json

            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="Low",
            confidence=60,
            finding="rewrite",
            file="app.py",
            line=6,
            start_line=4,
            suggested_code="new_4\nnew_5\nnew_6",
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.inline_posted == 1
    body = posts[0]["body"]
    # GitLab multi-line fence: lines_above = line - start_line = 6 - 4 = 2
    assert "```suggestion:-2+0" in body
    assert "new_4" in body


def test_post_findings_drops_unsafe_suggestion() -> None:
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/discussions" in str(req.url):
            import json

            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="Low",
            confidence=60,
            finding="injected",
            file="app.py",
            line=4,
            suggested_code="bad\n```\nescape",
        )
    ]
    prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    body = posts[0]["body"]
    assert "```suggestion" not in body
    # The body itself shouldn't carry triple-backticks at all
    assert "```" not in body


def test_post_findings_max_inline_cap() -> None:
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append({})
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(severity="Low", confidence=50, finding=f"f{i}", file="app.py", line=4 + (i % 3))
        for i in range(10)
    ]
    result = prov.post_findings(
        findings,
        DiffContext(diff_text=_DIFF, head_sha=_HEAD),
        event="COMMENT",
        max_inline=3,
    )
    assert result.inline_posted == 3
    assert result.body_findings == 7


def test_post_findings_400_falls_back_to_body() -> None:
    """GitLab returns 400 when position is invalid (line not in MR diff)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/discussions" in str(req.url):
            return httpx.Response(400, json={"message": "position invalid"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [Finding(severity="High", confidence=90, finding="x", file="app.py", line=4)]
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    # All inline posts failed → degraded; counts the finding as body
    assert result.inline_posted == 0
    assert result.body_findings == 1
    assert result.error is not None and "all discussion posts failed" in result.error
