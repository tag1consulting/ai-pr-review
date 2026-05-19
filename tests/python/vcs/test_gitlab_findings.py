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


# ---------------------------------------------------------------------------
# token_table appended to summary note in post_findings
# ---------------------------------------------------------------------------

_SUMMARY_MARKER = "<!-- ai-pr-review-summary sha=abc -->"
_EXISTING_NOTE_BODY = f"{_SUMMARY_MARKER}\n## PR Summary\n\nWalkthrough here."


def test_post_findings_appends_token_table_to_summary_note() -> None:
    """When token_table is non-empty, post_findings must PUT the updated summary note."""
    import json

    put_bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/notes" in str(req.url):
            return httpx.Response(200, json=[{"id": 55, "body": _EXISTING_NOTE_BODY}])
        if req.method == "PUT" and "/notes/55" in str(req.url):
            put_bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 55})
        if req.method == "POST" and "/discussions" in str(req.url):
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [Finding(severity="Low", confidence=80, finding="x", file="app.py", line=4)]
    accordion = "<details>\n<summary>Token usage by agent</summary>\n\ntable\n</details>"
    prov.post_findings(
        findings,
        DiffContext(diff_text=_DIFF, head_sha=_HEAD),
        event="REQUEST_CHANGES",
        token_table=accordion,
    )

    assert put_bodies, "PUT to update summary note must have been called"
    new_body = put_bodies[-1]["body"]
    assert "## PR Summary" in new_body
    assert accordion in new_body


def test_post_findings_token_table_strips_old_accordion() -> None:
    """A previous run's accordion must be replaced, not doubled."""
    import json

    old_accordion = "<details>\n<summary>Token usage by agent</summary>\n\nold\n</details>"
    stored = f"{_SUMMARY_MARKER}\n## PR Summary\n\n{old_accordion}"
    put_bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/notes" in str(req.url):
            return httpx.Response(200, json=[{"id": 55, "body": stored}])
        if req.method == "PUT" and "/notes/55" in str(req.url):
            put_bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 55})
        if req.method == "POST" and "/discussions" in str(req.url):
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    new_accordion = "<details>\n<summary>Token usage by agent</summary>\n\nnew\n</details>"
    prov.post_findings(
        [],
        DiffContext(diff_text=_DIFF, head_sha=_HEAD),
        event="COMMENT",
        token_table=new_accordion,
    )

    assert put_bodies, "PUT must have been called"
    new_body = put_bodies[-1]["body"]
    assert "old" not in new_body
    assert "new" in new_body


def test_post_findings_token_table_no_summary_note_skips_put() -> None:
    """When no summary note exists, no PUT is issued."""
    put_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/notes" in str(req.url):
            return httpx.Response(200, json=[])
        if req.method == "PUT":
            put_calls.append(str(req.url))
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)

    prov = _make_provider(handler)
    prov.post_findings(
        [],
        DiffContext(diff_text=_DIFF, head_sha=_HEAD),
        event="COMMENT",
        token_table="<details>table</details>",
    )
    assert put_calls == [], "no PUT should be issued when no summary note found"


def test_post_findings_token_table_http_error_is_failsoft() -> None:
    """HTTP 4xx on the summary note PUT must not abort post_findings."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/notes" in str(req.url):
            return httpx.Response(200, json=[{"id": 55, "body": _EXISTING_NOTE_BODY}])
        if req.method == "PUT" and "/notes/55" in str(req.url):
            return httpx.Response(403, json={"message": "forbidden"})
        if req.method == "POST" and "/discussions" in str(req.url):
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [Finding(severity="High", confidence=90, finding="x", file="app.py", line=4)]
    result = prov.post_findings(
        findings,
        DiffContext(diff_text=_DIFF, head_sha=_HEAD),
        event="REQUEST_CHANGES",
        token_table="<details>table</details>",
    )
    # Review still completes despite PUT failure
    assert result.error is None
    assert result.inline_posted == 1
