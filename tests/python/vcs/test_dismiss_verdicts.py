"""Verdict-marker recording — the write side of the canonical-review-reuse
cross-run dedup design (see #714's PR description). Covers
`_fingerprint_for_finding_id` and `_record_verdict`'s wiring into
`dismiss_by_finding_id` / `dismiss_inline_reply`.

Follows the `_make_provider(handler)` harness from `test_dismiss_github.py`.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.slash.dismiss import dismiss_by_finding_id, dismiss_inline_reply
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs._finding_ids import fingerprint
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider, _build_inline_comment_body
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
    build_id_map_marker,
    build_verdicts_marker,
    extract_verdicts,
)


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
                body = _json.loads(request.content)
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


def _finding(text: str, source: str = "code-reviewer", file: str = "app.py", line: int = 10) -> Finding:
    return Finding(severity="medium", confidence=80, finding=text, source=source, file=file, line=line)


def _put_bodies(rec: _Recorder, path_suffix: str) -> list[str]:
    return [c[2]["body"] for c in rec.calls if c[0] == "PUT" and c[1].endswith(path_suffix) and c[2]]


# ---------------------------------------------------------------------------
# dismiss_by_finding_id — BODY branch
# ---------------------------------------------------------------------------


def test_body_fixed_records_fixed_verdict() -> None:
    f = _finding("SQLi risk", source="blind", file="db.py", line=12)
    bullet = format_body_finding(f, finding_id=7)
    review_body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 9, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "PUT" and str(req.url).endswith("/reviews/9"):
            return httpx.Response(200, json={"id": 9})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 7, actor="alice", command="fixed", commit_sha="a1b2c3d")

    assert result.errors == ()
    puts = _put_bodies(rec, "/reviews/9")
    assert len(puts) == 1
    assert extract_verdicts(puts[0]) == {fingerprint(f): "fixed"}


# ---------------------------------------------------------------------------
# Canonical-review selection: highest id among all bot reviews, not
# necessarily the one carrying the finding or the one whose thread resolves.
# ---------------------------------------------------------------------------


def test_verdict_recorded_on_highest_id_review_not_the_finding_source() -> None:
    """The canonical review is the most-recently-posted bot review PERIOD --
    even if it's a different, newer review than the one that originally
    carried the finding being dismissed."""
    f = _finding("style nit", source="phpcs", file="legacy.py", line=5)
    bullet = format_body_finding(f, finding_id=2)
    old_review_body = "### Findings not attached to specific lines\n\n" + bullet + "\n"
    newer_review_body = "## AI Review: Approved\n\nNo findings above the confidence threshold."

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[
                    {"id": 5, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": old_review_body},
                    {"id": 11, "state": "APPROVED", "user": {"login": "github-actions[bot]"}, "body": newer_review_body},
                ],
            )
        if req.method == "PUT" and str(req.url).endswith("/reviews/11"):
            return httpx.Response(200, json={"id": 11})
        if req.method == "PUT" and str(req.url).endswith("/reviews/5"):
            raise AssertionError("verdict must be recorded on the canonical (highest-id) review, not the source one")
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 2, actor="alice", command="dismiss")

    assert result.errors == ()
    puts = _put_bodies(rec, "/reviews/11")
    assert len(puts) == 1
    assert extract_verdicts(puts[0]) == {fingerprint(f): "dismissed"}
    # The canonical review's own (unrelated) content must survive the patch.
    assert "No findings above the confidence threshold" in puts[0]


def test_verdict_marker_upserts_into_existing_marker_without_duplicating() -> None:
    """A canonical review that already carries a verdicts marker (from an
    earlier dismiss on a different finding) must have that marker patched
    in place, preserving the prior entry, not appended a second time."""
    f = _finding("missing null check", source="code-reviewer", file="app.py", line=9)
    bullet = format_body_finding(f, finding_id=4)
    prior_fp = "adversarial-general|other.py|1|aaaaaaaaaaaa"
    review_body = (
        "### Findings not attached to specific lines\n\n"
        + bullet
        + "\n"
        + build_verdicts_marker({prior_fp: "dismissed"})
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 3, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "PUT" and str(req.url).endswith("/reviews/3"):
            return httpx.Response(200, json={"id": 3})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 4, actor="alice", command="wont-fix")

    assert result.errors == ()
    puts = _put_bodies(rec, "/reviews/3")
    assert len(puts) == 1
    assert puts[0].count("ai-pr-review-verdicts:") == 1
    assert extract_verdicts(puts[0]) == {
        prior_fp: "dismissed",
        fingerprint(f): "dismissed",
    }


# ---------------------------------------------------------------------------
# Failure isolation: verdict-recording failures never leak into
# DismissResult.errors, but are still observable via logging.
# ---------------------------------------------------------------------------


def test_verdict_put_failure_does_not_leak_into_dismiss_result_errors(caplog) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
    bullet = format_body_finding(f, finding_id=3)
    review_body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 1, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "PUT" and str(req.url).endswith("/reviews/1"):
            return httpx.Response(422, text="Validation Failed")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.slash.dismiss"):
        result = dismiss_by_finding_id(prov, 3, actor="alice", command="dismiss")

    # The primary outcome (reply, feedback routing) is completely unaffected
    # by the verdict-recording failure.
    assert result.errors == ()
    assert result.feedback_source == "phpcs"
    assert any("failed to record" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# dismiss_inline_reply — verdict recorded from the comment's own F<n> token
# ---------------------------------------------------------------------------


def _inline_thread(
    tid: str, *, resolved: bool, body: str, comment_db_id: int | None = None, review_db_id: int | None = None
) -> dict:
    inner: dict = {"body": body, "author": {"login": "github-actions[bot]"}}
    if comment_db_id is not None:
        inner["databaseId"] = comment_db_id
    inner["pullRequestReview"] = {"databaseId": review_db_id} if review_db_id is not None else None
    return {"id": tid, "isResolved": resolved, "comments": {"nodes": [inner]}}


def _threads_response(nodes: list[dict]) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def test_dismiss_inline_reply_records_verdict_from_comments_own_fid() -> None:
    # Inline findings have no body-level bullet to reconstruct a fingerprint
    # from (`_parse_existing_ids`'s fallback path only covers body findings),
    # so the reverse lookup depends entirely on the id-map marker every real
    # review embeds (`github.py`'s `assemble_id_map`/`build_id_map_marker`,
    # which covers inline and body findings alike).
    f = _finding("leaked secret", source="security-reviewer", file="config.py", line=3)
    comment_body = _build_inline_comment_body(f, finding_id=6)
    nodes = [_inline_thread("T1", resolved=False, body=comment_body, comment_db_id=77, review_db_id=41)]
    review_body = (
        "## AI Review Findings\n\n"
        + comment_body
        + "\n"
        + build_id_map_marker({fingerprint(f): 6})
    )

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "COMMENTED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(
                200,
                json=[{"id": 41, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "PUT" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_inline_reply(prov, 77, None, actor="alice", command="false-positive")

    assert result.thread_resolved is True
    puts = _put_bodies(rec, "/reviews/41")
    assert len(puts) == 1
    assert extract_verdicts(puts[0]) == {fingerprint(f): "dismissed"}
