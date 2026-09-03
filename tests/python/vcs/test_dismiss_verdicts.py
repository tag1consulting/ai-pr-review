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
from ai_pr_review.slash.dismiss import (
    FindingLocation,
    _fingerprint_for_finding_id,
    classify_finding,
    dismiss_by_finding_id,
    dismiss_inline_reply,
)
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


def test_verdict_put_targets_newest_non_empty_review_not_implicit_reply_review() -> None:
    """GitHub auto-creates a `body: ""` review authored by the bot every time
    it replies to an inline comment via the REST replies endpoint (the
    severity-escalation notice, the recurred-finding reply, the
    feedback-command reply). Such a review is routinely the highest id --
    confirmed live on a real PR, where three of these implicit reviews
    landed after the real CHANGES_REQUESTED review that carried the
    finding. Without the empty-body filter in `select_canonical`, the verdict
    PUT lands on the empty review and GitHub rejects it with HTTP 422
    ("Could not edit a review with a missing body"), silently dropping the
    verdict. The PUT must instead land on review 9 (the newest non-empty
    body), never on review 12."""
    f = _finding("style nit", source="phpcs", file="legacy.py", line=5)
    bullet = format_body_finding(f, finding_id=2)
    review_body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[
                    {"id": 9, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": review_body},
                    {"id": 12, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": ""},
                ],
            )
        if req.method == "PUT" and str(req.url).endswith("/reviews/9"):
            return httpx.Response(200, json={"id": 9})
        if req.method == "PUT" and str(req.url).endswith("/reviews/12"):
            raise AssertionError("verdict must never target the empty-body implicit review")
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 2, actor="alice", command="dismiss")

    assert result.errors == ()
    puts = _put_bodies(rec, "/reviews/9")
    assert len(puts) == 1
    assert extract_verdicts(puts[0]) == {fingerprint(f): "dismissed"}


def test_record_verdict_seeds_from_union_not_just_canonical_body() -> None:
    """A verdict recorded on an older review must survive a later,
    marker-less review (e.g. `GitHubProvider.submit_approval`'s human-facing
    "auto-approved" message, issue #590) becoming canonical -- `_record_verdict`
    must seed from `merge_verdicts(reviews)` (every prior body), not just
    `extract_verdicts(canonical.body)`, or the marker-less review silently
    erases every earlier verdict the next time one is written."""
    older_verdict_body = (
        "## AI Review Findings\n\nsome body\n"
        + build_verdicts_marker({"already-dismissed-fp": "dismissed"})
    )
    f = _finding("new nit", source="code-reviewer", file="app.py", line=20)
    bullet = format_body_finding(f, finding_id=9)
    marker_less_canonical_body = "Auto-approved: all findings resolved."

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 5, "state": "DISMISSED",
                        "user": {"login": "github-actions[bot]"},
                        "body": "### Findings not attached to specific lines\n\n" + bullet + "\n",
                    },
                    {
                        "id": 6, "state": "DISMISSED",
                        "user": {"login": "github-actions[bot]"},
                        "body": older_verdict_body,
                    },
                    {
                        "id": 11, "state": "APPROVED",
                        "user": {"login": "github-actions[bot]"},
                        "body": marker_less_canonical_body,
                    },
                ],
            )
        if req.method == "PUT" and str(req.url).endswith("/reviews/11"):
            return httpx.Response(200, json={"id": 11})
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 9, actor="alice", command="fixed", commit_sha="a1b2c3d")

    assert result.errors == ()
    puts = _put_bodies(rec, "/reviews/11")
    assert len(puts) == 1
    verdicts = extract_verdicts(puts[0])
    # The new verdict was recorded...
    assert verdicts[fingerprint(f)] == "fixed"
    # ...and the older review's verdict survived the union-seed, rather than
    # being silently dropped because the canonical body itself carried no
    # verdicts marker at all.
    assert verdicts["already-dismissed-fp"] == "dismissed"


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


def test_verdict_put_failure_warning_goes_to_stderr_not_stdout(
    monkeypatch, capsys
) -> None:
    """`dismiss`/`dismiss-inline` (ai_pr_review/cli.py) print the human-facing
    reply to stdout, which the calling workflow captures verbatim and posts
    as the PR comment. A verdict-recording-failure `::warning::` annotation
    printed to stdout (an earlier version of `_warn_verdict_failure` did
    exactly this) lands inside that posted comment instead of the Actions
    log -- confirmed live on a real PR. `dismiss_by_finding_id` itself never
    touches stdout, so this asserts directly on `_warn_verdict_failure`'s
    stream choice rather than on `click.echo`'s (a CLI-level concern)."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
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
    result = dismiss_by_finding_id(prov, 3, actor="alice", command="dismiss")

    assert result.errors == ()
    captured = capsys.readouterr()
    assert "::warning::" not in captured.out
    assert "::warning::" in captured.err


def test_verdict_put_raising_httpx_error_does_not_crash_dismiss(caplog) -> None:
    """`update_review_body` doesn't always fail with a tidy non-2xx response --
    `RecordingClient.request` routes through `retry_transient`, which *raises*
    on retry exhaustion or a non-transient transport error. `_record_verdict`
    must swallow that raise too, not just the `ok is False` tuple case, or a
    network blip on this purely-additive side channel crashes the whole
    dismiss/fixed command after the primary action already succeeded."""
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
            raise httpx.ConnectError("connection reset", request=req)
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.slash.dismiss"):
        result = dismiss_by_finding_id(prov, 3, actor="alice", command="dismiss")

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


def test_dismiss_inline_reply_verdict_lookup_list_reviews_raising_is_swallowed(caplog) -> None:
    """The ad-hoc `list_bot_reviews()` call `dismiss_inline_reply` makes
    purely to resolve a verdict fingerprint can also raise (same
    retry_transient behavior as the PUT path) -- confirm the thread still
    resolves and no exception escapes."""
    f = _finding("leaked secret", source="security-reviewer", file="config.py", line=3)
    comment_body = _build_inline_comment_body(f, finding_id=6)
    nodes = [_inline_thread("T1", resolved=False, body=comment_body, comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        # get_review_state's single-review GET (used by _dismiss_if_all_resolved,
        # unrelated to verdict recording) must be handled BEFORE the generic
        # "/reviews" list-endpoint branch below, since "/reviews/41" contains
        # "/reviews" as a substring. Returning a non-CHANGES_REQUESTED state
        # makes that call a clean no-op so this test isolates the verdict-lookup
        # list_bot_reviews() failure specifically.
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "COMMENTED"})
        if req.method == "GET" and "/reviews" in url:
            raise httpx.ConnectError("connection reset", request=req)
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.slash.dismiss"):
        result = dismiss_inline_reply(prov, 77, None, actor="alice", command="false-positive")

    assert result.thread_resolved is True
    assert result.errors == ()
    assert any("verdict lookup failed listing reviews" in msg for msg in caplog.messages)


def test_dismiss_inline_reply_verdict_lookup_http_failure_is_logged_not_silently_dropped(caplog) -> None:
    """A non-2xx (not raised) list_bot_reviews() failure during verdict lookup
    is truncated from provider._errors (must not surface as a resolve/dismiss
    error) but must still be logged -- silently deleting the diagnostic with
    no trace anywhere would make a real permissions/rate-limit problem
    invisible."""
    f = _finding("leaked secret", source="security-reviewer", file="config.py", line=3)
    comment_body = _build_inline_comment_body(f, finding_id=6)
    nodes = [_inline_thread("T1", resolved=False, body=comment_body, comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        # See the sibling raising-test above for why this branch must precede
        # the generic "/reviews" check.
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "COMMENTED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.slash.dismiss"):
        result = dismiss_inline_reply(prov, 77, None, actor="alice", command="false-positive")

    assert result.thread_resolved is True
    assert result.errors == ()
    assert any(
        "verdict lookup list_bot_reviews failed" in msg and "403" in msg
        for msg in caplog.messages
    )


# ---------------------------------------------------------------------------
# _fingerprint_for_finding_id vs. classify_finding: documented, safe
# divergence when the id-map marker is present but incomplete.
# ---------------------------------------------------------------------------


def test_fingerprint_lookup_safely_diverges_from_classify_when_marker_omits_the_id() -> None:
    """`classify_finding` unconditionally bullet-scans every body first
    (`_scan_body_bullets`), so it finds F5 as BODY regardless of the marker.
    `_fingerprint_for_finding_id` reuses `_parse_existing_ids`, which takes a
    marker fast-path and skips bullet-scanning entirely whenever a body's
    id-map marker is non-empty -- even if that marker doesn't happen to
    include the specific ID being looked up. This is a real, reachable
    divergence (an id-map marker can in principle omit an id a bullet still
    carries), but it degrades safely: `_record_verdict` no-ops on a `None`
    fingerprint rather than guessing, so this locks down that the primary
    dismiss outcome is completely unaffected even when the two lookups
    disagree."""
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
    bullet = format_body_finding(f, finding_id=5)
    # Marker is non-empty (triggers the fast-path) but maps a different
    # fingerprint entirely -- it does not carry F5's fingerprint.
    incomplete_marker = build_id_map_marker({"other|other.py|1|deadbeefcafe": 9})
    review_body = (
        "### Findings not attached to specific lines\n\n"
        + bullet
        + "\n"
        + incomplete_marker
    )

    assert classify_finding([review_body], 5).location == FindingLocation.BODY
    assert _fingerprint_for_finding_id([review_body], 5) is None

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 1, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "PUT":
            raise AssertionError("no verdict PUT expected when the fingerprint lookup can't resolve")
        return httpx.Response(404)

    prov, _ = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 5, actor="alice", command="dismiss")

    assert result.errors == ()
    assert result.feedback_source == "phpcs"


# ---------------------------------------------------------------------------
# End-to-end incident reproduction: a finding that moved from an out-of-diff
# body bullet (older review) to an inline thread (newest review), dismissed
# via a top-level `/ai-pr-review dismiss F<n>` comment, with a reply-created
# empty-body review sitting at the highest id in between.
# ---------------------------------------------------------------------------


def test_dismiss_by_finding_id_moved_to_inline_resolves_and_dismisses() -> None:
    """Reproduces the live incident this fix addresses. F15 was an
    out-of-diff Low body bullet in an older APPROVED review (id 8). The next
    run rendered it as an inline High finding (id-map only) with its own
    open thread on a newer CHANGES_REQUESTED review (id 9). GitHub also
    auto-created an empty-body COMMENTED review (id 12, from an unrelated
    bot reply) that is the highest id of the three. Before this fix,
    `classify_finding` scanned oldest-first, found the stale body bullet,
    and took the body-only branch: no thread fetch, no resolve, no review
    dismissal, and the verdict PUT (had it even been attempted) would 422
    against review 12. After this fix: F15 classifies INLINE from the
    newest review, the thread resolves, the verdict lands on review 9 (never
    12), and review 9 -- now fully resolved -- is dismissed."""
    old_bullet = format_body_finding(
        Finding(severity="low", confidence=80, finding="secret", source="trufflehog", file="hubspotForm.js", line=62),
        finding_id=15,
    )
    old_body = "<details>\n<summary>Out-of-diff analyzer findings (1)</summary>\n\n" + old_bullet + "\n</details>"

    inline_finding = Finding(
        severity="high", confidence=80, finding="secret", source="trufflehog", file="hubspotForm.js", line=62
    )
    comment_body = _build_inline_comment_body(inline_finding, finding_id=15)
    new_body = (
        "## AI Review Findings\n\n"
        + comment_body
        + "\n"
        + build_id_map_marker({fingerprint(inline_finding): 15})
    )

    nodes = [_inline_thread("T1", resolved=False, body=comment_body, comment_db_id=77, review_db_id=9)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "GET" and url.endswith("/reviews/9"):
            return httpx.Response(200, json={"id": 9, "state": "CHANGES_REQUESTED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(
                200,
                json=[
                    {"id": 8, "state": "APPROVED", "user": {"login": "github-actions[bot]"}, "body": old_body},
                    {"id": 9, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": new_body},
                    {"id": 12, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": ""},
                ],
            )
        if req.method == "PUT" and url.endswith("/reviews/9"):
            return httpx.Response(200, json={"id": 9})
        if req.method == "PUT" and url.endswith("/reviews/9/dismissals"):
            return httpx.Response(200, json={"id": 9, "state": "DISMISSED"})
        if req.method == "PUT" and (url.endswith("/reviews/12") or url.endswith("/reviews/12/dismissals")):
            raise AssertionError("must never write to the empty-body implicit review")
        return httpx.Response(404)

    prov, rec = _make_provider(handler)
    result = dismiss_by_finding_id(prov, 15, actor="alice", command="dismiss")

    assert result.errors == ()
    assert result.thread_resolved is True
    assert result.review_dismissed is True
    # BODY-branch side effects (feedback-store routing) must NOT have fired --
    # this is the INLINE path.
    assert result.feedback_source == ""

    puts = _put_bodies(rec, "/reviews/9")
    assert len(puts) == 1
    assert extract_verdicts(puts[0]) == {fingerprint(inline_finding): "dismissed"}
    assert any(c[0] == "PUT" and c[1].endswith("/reviews/9/dismissals") for c in rec.calls)
