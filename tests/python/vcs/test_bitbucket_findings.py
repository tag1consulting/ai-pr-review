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


def test_post_findings_demoted_to_body_high_counts_in_headline() -> None:
    """Regression test for #622 on Bitbucket: a judge-downranked High finding
    (demoted_to_body=True) must count at its true severity in the headline —
    matching GitHub's behavior and review.outcome.classify_review_outcome's
    decision for the same finding."""
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
            confidence=65,
            finding="author_association is not a reliable authorization check",
            source="code-reviewer",
            file=".github/workflows/ai-pr-review.yml",
            line=195,
            demoted_to_body=True,
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok

    raw = captured[0]["content"]["raw"]
    assert "**Overall Risk:** High" in raw, (
        f"demoted_to_body must not hide a High finding from the headline risk; got: {raw[:400]!r}"
    )
    assert "**Findings:** 1" in raw, (
        f"demoted_to_body must not exclude the finding from the headline count; got: {raw[:400]!r}"
    )


def test_post_findings_approve_with_only_out_of_diff_findings_still_renders_them() -> None:
    """Regression test: an all-out_of_diff finding set combined with event=APPROVE
    must NOT blank the rendered body. compute_headline() excludes genuine
    out_of_diff findings from finding_total/risk (its documented headline-count
    convention, shared with GitHub's collapsed-section logic) -- but per
    _render_combined_body's own docstring, Bitbucket has no collapsed section,
    so out_of_diff findings must still appear in the flat findings_block.
    Gating the "no findings" branch on finding_total == 0 instead of on the raw
    findings list would blank findings_block whenever every finding happens to
    be out_of_diff (always Low severity, by apply_diff_scope's invariant), so
    APPROVE + all-out_of_diff would silently drop real findings from the
    rendered body while still claiming "No findings ... look good" -- the
    exact class of bug #622 fixed, reintroduced on Bitbucket specifically by
    an incomplete first pass at this fix."""
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
            severity="Low",
            confidence=80,
            finding="style nit outside the diff",
            source="phpcs",
            file="legacy.php",
            line=900,
            out_of_diff=True,
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="APPROVE"
    )
    assert result.ok

    raw = captured[0]["content"]["raw"]
    assert "style nit outside the diff" in raw, (
        "an out_of_diff finding must still render in Bitbucket's flat "
        f"findings_block even when finding_total==0 triggers APPROVE; got: {raw[:400]!r}"
    )
    # The summary line must not claim "Findings: 0" directly above a rendered,
    # non-empty findings list -- that reintroduces the #622 bug one level
    # deeper: a technically-non-empty count (0) that contradicts the content
    # right below it. "0 in the diff" is the intentionally qualified wording.
    assert "**Findings:** 0 in the diff" in raw, (
        f"summary line must not read a bare 'Findings: 0' beside a rendered "
        f"out_of_diff finding; got: {raw[:400]!r}"
    )
    assert "**Findings:** 0\n" not in raw and "**Findings:** 0\n\n" not in raw, (
        f"bare 'Findings: 0' would contradict the rendered finding below it; got: {raw[:400]!r}"
    )


def test_post_findings_comment_with_only_out_of_diff_findings_does_not_contradict() -> None:
    """Same class of bug as the APPROVE case above, reachable via event=COMMENT:
    an all-out_of_diff finding set makes compute_headline() return risk="None"
    (not "Unknown", since no agents failed), so the COMMENT+Unknown+empty
    branch doesn't catch it and it falls to the generic trailing branch, which
    must also avoid a bare "Findings: 0" beside the rendered finding."""
    captured: list[dict] = []
    existing = _existing_summary(comment_id=43)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 43})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="Low",
            confidence=80,
            finding="another style nit outside the diff",
            source="phpcs",
            file="legacy.php",
            line=901,
            out_of_diff=True,
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok

    raw = captured[0]["content"]["raw"]
    assert "another style nit outside the diff" in raw
    assert "**Findings:** 0 in the diff" in raw, (
        f"summary line must not read a bare 'Findings: 0' beside a rendered "
        f"out_of_diff finding; got: {raw[:400]!r}"
    )


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
