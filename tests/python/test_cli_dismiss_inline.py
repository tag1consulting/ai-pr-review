"""Tests for the `ai-pr-review dismiss-inline` CLI subcommand (story 13-3).

Wires story 13-1's `dismiss_inline_reply` to the CLI. Follows the
`_make_provider(handler)` HTTP-mocking harness established in
`tests/python/vcs/test_dismiss_github.py`, invoked through Click's CliRunner.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from click.testing import CliRunner

import ai_pr_review.vcs as vcs_module
from ai_pr_review.cli import cli
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER


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
        retry_policy=RetryPolicy(attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    return GitHubProvider(config=config, client=client), rec


def _inline_thread(
    tid: str,
    *,
    resolved: bool,
    body: str,
    comment_db_id: int | None = None,
    review_db_id: int | None = None,
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


def _base_args(
    parent_comment_id: int, review_id: int | None = None, command: str = "dismiss"
) -> list[str]:
    args = [
        "dismiss-inline",
        "--parent-comment-id",
        str(parent_comment_id),
        "--actor",
        "alice",
        "--command",
        command,
        "--pr-number",
        "5",
    ]
    if review_id is not None:
        args += ["--review-id", str(review_id)]
    return args


def test_resolves_thread_and_dismisses_review_when_review_id_given(monkeypatch) -> None:
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            dismissed.append(url)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]


def test_dismiss_put_failure_surfaces_as_warning_not_silent(monkeypatch) -> None:
    # A resolve that succeeds but a dismiss PUT that fails (e.g. the review
    # is no longer CHANGES_REQUESTED) must not be reported to the user as a
    # clean "resolved the thread" with the failure swallowed -- the CLI must
    # surface DismissResult.errors on stderr so it lands in the workflow log,
    # rather than repeating the #555 class of error this epic exists to kill.
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        # State check reports CHANGES_REQUESTED so the dismiss PUT is still
        # attempted (and fails) -- this test covers the PUT-failure surfacing
        # path specifically, not the story-13-5 skip-on-wrong-state path.
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(422, json={"message": "Review is not in a dismissable state"})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "dismiss review 41" in result.stderr
    assert "422" in result.stderr


def test_missing_review_id_falls_back_to_thread_review_and_still_resolves(monkeypatch) -> None:
    # Note: a parent comment with no resolvable pull_request_review_id implies
    # the thread it belongs to also carries no review (the comment's review
    # membership is the same fact either way), so `dismiss_inline_reply`'s
    # thread-derived fallback (`review_id or _thread_review_id(thread)`) is
    # not reachable with a "None review_id, real review on the thread" state
    # in production — every real inline comment carries a review id. When the
    # CLI omits --review-id, dismiss_inline_reply falls back to deriving it
    # from the thread itself, so a real review_db_id on the thread still
    # yields a dismissal here.
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            dismissed.append(url)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55))

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]


def test_no_review_anywhere_resolves_without_dismissal(monkeypatch) -> None:
    # The genuinely reachable "no review to target" case: neither the parent
    # comment nor the thread it belongs to carries a review id. Resolution
    # still succeeds; there is nothing to check "all resolved" against, so no
    # dismissal PUT is attempted.
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=None)]
    dismissed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in str(req.url):
            dismissed.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55))

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert dismissed == []


def test_thread_not_found_reports_confused(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response([]))
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(999, review_id=41))

    assert result.exit_code == 0, result.output
    assert "could not find the review thread" in result.stdout
    assert "::notice::reaction=confused" in result.stderr
    assert "::notice::reaction=confused" not in result.stdout


def test_reaction_marker_done_on_stderr_only(monkeypatch) -> None:
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in str(req.url):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert "::notice::reaction=done" in result.stderr
    assert "::notice::reaction=done" not in result.stdout


def test_other_bots_thread_ignored_reports_confused(monkeypatch) -> None:
    other_body = "[High] leak\nnot from us"
    nodes = [_inline_thread("T1", resolved=False, body=other_body, comment_db_id=88, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(88, review_id=41))

    assert result.exit_code == 0, result.output
    assert "not posted by this bot" in result.stdout
    assert "::notice::reaction=confused" in result.stderr


def test_non_github_provider_fails_closed_with_clear_message() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, _base_args(1, review_id=1), env={"VCS_PROVIDER": "gitlab"})

    assert result.exit_code != 0
    assert "GitHub-only" in result.stderr
    assert "gitlab" in result.stderr


def test_missing_required_option_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["dismiss-inline", "--command", "dismiss", "--pr-number", "5", "--actor", "alice"], env={}
    )

    assert result.exit_code != 0
