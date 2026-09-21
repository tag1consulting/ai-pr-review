"""Bitbucket parity Phase 3 (#839/#873): Code Insights annotations as the
inline display layer, and the id-map/verdict dedup gated by the same flag.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import CATEGORIES, Finding
from ai_pr_review.vcs._code_insights import _ANNOTATION_TYPE, build_annotation_payload
from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import SUMMARY_MARKER_PREFIX, build_verdicts_marker
from ai_pr_review.vcs.protocol import DiffContext

_HEAD = "abc1234def5678abc1234def5678abc1234def56"

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


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    code_insights: bool = True,
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
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t",
            code_insights=code_insights,
        ),
        client=client,
    )


def _existing_summary(comment_id: int = 100, body: str = "") -> dict:
    return {
        "id": comment_id,
        "content": {
            "raw": body or f"{SUMMARY_MARKER_PREFIX} sha={_HEAD} -->\n## AI Review: Approved\n\nNo findings yet.",
        },
    }


def _router(
    *,
    existing: dict,
    on_report_delete: Callable[[httpx.Request], httpx.Response] | None = None,
    on_report_put: Callable[[httpx.Request], httpx.Response] | None = None,
    on_annotations_post: Callable[[httpx.Request], httpx.Response] | None = None,
    on_comment_put: Callable[[httpx.Request], httpx.Response] | None = None,
    calls: list[tuple[str, str]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that routes by (method, path-shape) rather than method
    alone -- required once a single post_findings call issues more than one
    PUT (the report PUT and the comment PUT), unlike every pre-Phase-3
    Bitbucket test's handler."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if calls is not None:
            calls.append((req.method, url))
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if "/reports/ai-pr-review/annotations" in url and req.method == "POST":
            if on_annotations_post is not None:
                return on_annotations_post(req)
            return httpx.Response(200, json={})
        if "/reports/ai-pr-review" in url and req.method == "DELETE":
            if on_report_delete is not None:
                return on_report_delete(req)
            return httpx.Response(204)
        if "/reports/ai-pr-review" in url and req.method == "PUT":
            if on_report_put is not None:
                return on_report_put(req)
            return httpx.Response(200, json={"uuid": "{report}"})
        if req.method == "PUT":
            if on_comment_put is not None:
                return on_comment_put(req)
            return httpx.Response(200, json={"id": existing["id"]})
        return httpx.Response(404)

    return handler


def test_report_delete_then_put_then_post_annotations_in_order() -> None:
    calls: list[tuple[str, str]] = []
    existing = _existing_summary()
    handler = _router(existing=existing, calls=calls)
    prov = _make_provider(handler)
    findings = [
        Finding(severity="High", confidence=90, finding="sql injection", file="app.py", line=4),
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    non_get = [c for c in calls if c[0] != "GET"]
    methods_on_report = [m for m, u in non_get if "/reports/ai-pr-review" in u]
    assert methods_on_report[:3] == ["DELETE", "PUT", "POST"]


def test_annotation_payload_shape_and_f_id_token() -> None:
    captured: dict = {}

    def on_post(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    existing = _existing_summary()
    handler = _router(existing=existing, on_annotations_post=on_post)
    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="High", confidence=90, finding="sql injection",
            category="injection", file="app.py", line=4,
            remediation="use parameterized queries",
        ),
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert result.inline_posted == 1
    payload = captured["body"]
    assert isinstance(payload, list)
    assert len(payload) == 1
    annotation = payload[0]
    assert annotation["path"] == "app.py"
    assert annotation["line"] == 4
    assert annotation["annotation_type"] == "VULNERABILITY"
    assert annotation["severity"] == "HIGH"
    assert "[F1]" in annotation["summary"]
    assert "sql injection" in annotation["summary"]


def test_external_id_stable_across_two_runs_for_unchanged_finding() -> None:
    ids: list[str] = []

    def on_post(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        ids.append(body[0]["external_id"])
        return httpx.Response(200, json={})

    finding = Finding(severity="Medium", confidence=80, finding="missing null check", file="app.py", line=5)

    existing = _existing_summary()
    handler = _router(existing=existing, on_annotations_post=on_post)
    prov = _make_provider(handler)
    diff = DiffContext(diff_text=_DIFF, head_sha=_HEAD)
    r1 = prov.post_findings([finding], diff, event="COMMENT")
    assert r1.ok, r1.error
    r2 = prov.post_findings([finding], diff, event="COMMENT")
    assert r2.ok, r2.error
    assert len(ids) == 2
    assert ids[0] == ids[1]


def test_annotation_type_mapping_is_exhaustive_over_categories() -> None:
    assert set(_ANNOTATION_TYPE) == set(CATEGORIES)
    assert set(_ANNOTATION_TYPE.values()) <= {"VULNERABILITY", "BUG", "CODE_SMELL"}


def test_max_inline_cap_overflow_goes_to_body() -> None:
    findings = [
        Finding(severity="Low", confidence=70, finding=f"nit {i}", file="app.py", line=4)
        for i in range(3)
    ]
    existing = _existing_summary()
    handler = _router(existing=existing)
    prov = _make_provider(handler)
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT", max_inline=2,
    )
    assert result.ok, result.error
    assert result.inline_posted == 2
    assert result.body_findings == 1


def test_demoted_to_body_finding_never_goes_to_annotations() -> None:
    captured: dict = {}

    def on_post(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    findings = [
        Finding(
            severity="High", confidence=90, finding="judge-downranked",
            file="app.py", line=4, demoted_to_body=True,
        ),
    ]
    existing = _existing_summary()
    handler = _router(existing=existing, on_annotations_post=on_post)
    prov = _make_provider(handler)
    result = prov.post_findings(
        findings, DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.inline_posted == 0
    assert result.body_findings == 1
    assert "body" not in captured  # DELETE already cleared prior annotations; nothing to POST


def test_dismissed_verdict_suppresses_the_annotation() -> None:
    finding = Finding(severity="High", confidence=90, finding="false positive", file="app.py", line=4)
    from ai_pr_review.vcs._finding_ids import fingerprint

    fp = fingerprint(finding)
    body_with_verdict = (
        f"{SUMMARY_MARKER_PREFIX} sha={_HEAD} -->\n"
        "## AI Review: Approved\n\nNo findings yet.\n"
        + build_verdicts_marker({fp: "dismissed"})
    )
    captured: dict = {}

    def on_post(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    existing = _existing_summary(body=body_with_verdict)
    handler = _router(existing=existing, on_annotations_post=on_post)
    prov = _make_provider(handler)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.suppressed == 1
    assert result.inline_posted == 0
    assert result.body_findings == 0
    assert "body" not in captured  # DELETE already cleared prior annotations; nothing to POST


def test_code_insights_403_falls_back_to_body_rendering() -> None:
    def on_delete(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Code Insights not enabled on this plan")

    finding = Finding(severity="High", confidence=90, finding="sql injection", file="app.py", line=4)
    existing = _existing_summary()
    handler = _router(existing=existing, on_report_delete=on_delete)
    prov = _make_provider(handler)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert result.inline_posted == 0
    assert result.body_findings == 1
    assert any("code insights" in e.lower() for e in prov._errors)


def test_kill_switch_reproduces_pre_phase3_behavior() -> None:
    calls: list[tuple[str, str]] = []
    finding = Finding(severity="High", confidence=90, finding="sql injection", file="app.py", line=4)
    existing = _existing_summary()
    handler = _router(existing=existing, calls=calls)
    prov = _make_provider(handler, code_insights=False)
    result = prov.post_findings(
        [finding], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert result.inline_posted == 0
    assert result.body_findings == 1
    assert result.suppressed == 0
    non_get = [c for c in calls if c[0] != "GET"]
    assert not any("/reports/" in u for _m, u in non_get)


def test_build_annotation_payload_returns_none_without_file_or_line() -> None:
    no_file = Finding(severity="High", confidence=90, finding="x")
    assert build_annotation_payload(no_file, finding_id=1) is None
