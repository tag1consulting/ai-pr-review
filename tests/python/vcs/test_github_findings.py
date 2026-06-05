"""GitHubProvider.post_findings — inline anchoring, fallbacks, suggestions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER
from ai_pr_review.vcs.protocol import DiffContext


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[GitHubProvider, _Recorder]:
    rec = _Recorder()

    def _wrap(request: httpx.Request) -> httpx.Response:
        import json

        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = None
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
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    return GitHubProvider(config=config, client=client), rec


_VALID_SHA = "abc1234def5678abc1234def5678abc1234def56"

_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
 context_line_1
 context_line_2
 context_line_3
+added_line_4
+added_line_5
+added_line_6
"""


def _review_post_handler(review_id: int = 100, status: int = 201) -> Callable:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in str(req.url):
            return httpx.Response(status, json={"id": review_id, "state": "COMMENTED"})
        return httpx.Response(404)

    return handler


def test_post_findings_out_of_diff_goes_to_details_section() -> None:
    """out_of_diff findings must land in the <details> section, not inflate the
    headline count, and not appear in the main findings list."""
    # Use a body-only in-diff finding (no file/line) so it always lands in the
    # review body rather than as an inline comment.
    in_diff = Finding(
        severity="High",
        confidence=90,
        finding="real bug in diff",
        source="phpcs",
        out_of_diff=False,
    )
    ood = Finding(
        severity="Low",
        confidence=80,
        finding="pre-existing style issue",
        source="phpcs",
        file="app.py",
        line=99,
        out_of_diff=True,
    )
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    bodies: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        if req.method == "POST" and "/reviews" in str(req.url):
            body = json.loads(req.content) if req.content else {}
            bodies.append(body.get("body", ""))
            return httpx.Response(201, json={"id": 1, "state": "COMMENTED"})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_findings([in_diff, ood], diff, event="COMMENT")
    assert result.ok

    assert bodies, "no review body posted"
    body = bodies[0]

    # out-of-diff finding must appear in a <details> collapsed section
    assert "<details>" in body, "out-of-diff section must use <details>"
    assert "pre-existing style issue" in body, "ood finding text must appear in body"

    # The in-diff finding's text must appear in the main body
    assert "real bug in diff" in body

    # Headline count: 1 (in-diff only), not 2 (total).
    # The rendered format is "**Findings:** 1".
    assert "**Findings:** 1" in body, (
        "headline count must reflect in-diff findings only; got: %r" % body[:400]
    )
    assert "**Findings:** 2" not in body, "ood finding must not inflate the headline count"


def test_post_findings_inline_for_eligible_line() -> None:
    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="unsafe",
            source="blind",
            file="app.py",
            line=4,
            remediation="sanitize",
        ),
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    prov, rec = _make_provider(_review_post_handler())
    result = prov.post_findings(findings, diff, event="REQUEST_CHANGES")
    assert result.ok
    assert result.event == "REQUEST_CHANGES"
    assert result.inline_posted == 1
    assert result.body_findings == 0

    review_call = next(c for c in rec.calls if c[0] == "POST" and "/reviews" in c[1])
    payload = review_call[2]
    assert payload["event"] == "REQUEST_CHANGES"
    assert payload["commit_id"] == _VALID_SHA
    comments = payload["comments"]
    assert len(comments) == 1
    c = comments[0]
    assert c["path"] == "app.py"
    assert c["line"] == 4
    assert INLINE_MARKER in c["body"]
    assert "unsafe" in c["body"]
    assert "sanitize" in c["body"]


def test_post_findings_ineligible_line_falls_back_to_body() -> None:
    # Line 99 isn't in the diff at all
    findings = [
        Finding(
            severity="Medium",
            confidence=70,
            finding="too complex",
            source="adv",
            file="app.py",
            line=99,
        ),
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    prov, rec = _make_provider(_review_post_handler())
    result = prov.post_findings(findings, diff, event="COMMENT")
    assert result.ok
    assert result.inline_posted == 0
    assert result.body_findings == 1

    review_call = next(c for c in rec.calls if c[0] == "POST" and "/reviews" in c[1])
    payload = review_call[2]
    assert payload["comments"] == []
    assert "line not in diff" in payload["body"]


def test_post_findings_respects_max_inline_cap() -> None:
    findings = [
        Finding(
            severity="Low",
            confidence=60,
            finding=f"f{i}",
            source="adv",
            file="app.py",
            line=4 + (i % 3),
        )
        for i in range(10)
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    prov, rec = _make_provider(_review_post_handler())
    result = prov.post_findings(findings, diff, event="COMMENT", max_inline=3)
    assert result.inline_posted == 3
    # Remaining 7 go to body
    assert result.body_findings == 7


def test_post_findings_drops_suggestion_with_triple_backticks() -> None:
    findings = [
        Finding(
            severity="Low",
            confidence=60,
            finding="prompt injection attempt",
            file="app.py",
            line=4,
            suggested_code="good\n```\nescape",  # dangerous
        ),
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    prov, rec = _make_provider(_review_post_handler())
    result = prov.post_findings(findings, diff, event="COMMENT")
    assert result.inline_posted == 1
    review_call = next(c for c in rec.calls if c[0] == "POST" and "/reviews" in c[1])
    comment_body = review_call[2]["comments"][0]["body"]
    # The suggestion fence must NOT be emitted
    assert "```suggestion" not in comment_body
    assert "```" not in comment_body.replace("```suggestion", "")


def test_post_findings_approve_with_inline_splits_into_two_posts() -> None:
    findings = [
        Finding(
            severity="Low",
            confidence=70,
            finding="minor",
            file="app.py",
            line=5,
        )
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    post_bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in str(req.url):
            import json

            post_bodies.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 100})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_findings(findings, diff, event="APPROVE")
    assert result.ok
    # Two POSTs: one COMMENT with inline, one APPROVE body-only
    assert len(post_bodies) == 2
    assert post_bodies[0]["event"] == "COMMENT"
    assert len(post_bodies[0]["comments"]) == 1
    assert post_bodies[1]["event"] == "APPROVE"
    assert post_bodies[1]["comments"] == []


def test_post_findings_request_changes_retries_as_comment_on_failure() -> None:
    findings = [
        Finding(severity="High", confidence=90, finding="bad", file="app.py", line=4)
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    attempt = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in str(req.url):
            attempt["n"] += 1
            import json

            body = json.loads(req.content)
            if body["event"] == "REQUEST_CHANGES":
                return httpx.Response(
                    422,
                    json={"message": "can't request changes on own PR"},
                )
            # Second attempt as COMMENT succeeds
            return httpx.Response(201, json={"id": 200})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_findings(findings, diff, event="REQUEST_CHANGES")
    assert result.ok
    assert result.event == "COMMENT"
    assert result.degraded_to_comment is True
    assert attempt["n"] == 2


def test_post_findings_final_fallback_to_issue_comment() -> None:
    findings = [
        Finding(severity="High", confidence=90, finding="x", file="app.py", line=4)
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    def handler(req: httpx.Request) -> httpx.Response:
        # All review POSTs fail
        if req.method == "POST" and "/reviews" in str(req.url):
            return httpx.Response(422, json={"message": "broken"})
        # Issue-comment fallback succeeds
        if req.method == "POST" and "/issues/" in str(req.url) and "/comments" in str(req.url):
            return httpx.Response(201, json={"id": 777})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_findings(findings, diff, event="REQUEST_CHANGES")
    assert result.ok
    assert result.degraded_to_comment is True
    assert result.review_id is None


def test_post_findings_all_three_paths_fail_returns_error() -> None:
    findings = [
        Finding(severity="High", confidence=90, finding="x", file="app.py", line=4)
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    def handler(req: httpx.Request) -> httpx.Response:
        # Everything fails
        return httpx.Response(500, json={"message": "broken"})

    prov, _ = _make_provider(handler)
    result = prov.post_findings(findings, diff, event="REQUEST_CHANGES")
    assert not result.ok
    assert result.error is not None
    assert "posting attempts failed" in result.error


def test_approve_pre_review_failure_does_not_count_inline_posted() -> None:
    """B3: When the pre-APPROVE COMMENT review POST fails, inline_posted must be 0."""
    findings = [
        Finding(severity="Low", confidence=80, finding="x", file="app.py", line=4),
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and "/reviews" in str(req.url):
            # First call: pre-APPROVE COMMENT — fail
            # Second call: APPROVE body-only — succeed
            if not hasattr(handler, "_seen"):
                handler._seen = True  # type: ignore[attr-defined]
                return httpx.Response(422, json={"message": "no permission"})
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_findings(findings, diff, event="APPROVE")
    # The pre-APPROVE COMMENT POST failed, so inline_posted must be 0
    assert result.inline_posted == 0


def test_fallback_includes_inline_findings_when_approve_review_fails() -> None:
    """Fix: fallback body must include original inline findings even when APPROVE
    review fails after inline_comments was cleared for the APPROVE path."""
    findings = [
        Finding(severity="Low", confidence=80, finding="inline-x", file="app.py", line=4),
    ]
    diff = DiffContext(diff_text=_DIFF, head_sha=_VALID_SHA)
    fallback_bodies: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json

        if req.method == "POST" and "/reviews" in str(req.url):
            # pre-APPROVE COMMENT succeeds
            body = _json.loads(req.content) if req.content else {}
            if body.get("event") == "COMMENT" and body.get("comments"):
                return httpx.Response(201, json={"id": 1})
            # APPROVE body-only — fail so fallback fires
            return httpx.Response(403, json={"message": "no permission"})
        if req.method == "POST" and "/issues/" in str(req.url) and "/comments" in str(req.url):
            # issue-comment fallback
            body = _json.loads(req.content) if req.content else {}
            fallback_bodies.append(body.get("body", ""))
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = prov.post_findings(findings, diff, event="APPROVE")
    assert result.degraded_to_comment is True
    # The fallback body must contain the inline finding text
    assert any("inline-x" in b for b in fallback_bodies), (
        "fallback body must include original inline findings"
    )
