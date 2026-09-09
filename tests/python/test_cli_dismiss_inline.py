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
from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider, _build_inline_comment_body
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
    path: str | None = None,
) -> dict:
    inner: dict = {"body": body, "author": {"login": "github-actions[bot]"}}
    if comment_db_id is not None:
        inner["databaseId"] = comment_db_id
    inner["pullRequestReview"] = {"databaseId": review_db_id} if review_db_id is not None else None
    node: dict = {"id": tid, "isResolved": resolved, "comments": {"nodes": [inner]}}
    if path is not None:
        node["path"] = path
    return node


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


def test_resolve_thread_401_emits_checks_tab_annotation_but_exit_0(
    monkeypatch,
) -> None:
    """#611: a 401 on the resolveReviewThread GraphQL mutation must not be a
    silent green success. The command must still exit 0 (so the workflow step
    still writes reply_b64 and posts the fallback reply via a different,
    working token -- see the dismiss/dismiss-inline docstrings), but it must
    also emit a GitHub Actions ::error:: annotation so the failure is visible
    on the PR's Checks tab, not just as a ::warning:: buried in the run log.
    """
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert result.exit_code == 0, result.output
    assert "::warning::dismiss-inline: resolve thread" in result.stderr
    assert "401" in result.stderr
    assert "::error::ai-pr-review dismiss-inline:" in result.stderr
    assert "NOT dismissed/resolved" in result.stderr
    # The raw provider error text must not be duplicated into the annotation.
    assert result.stderr.count("Bad credentials") == 1


def test_dismiss_put_failure_annotation_does_not_claim_finding_unresolved(monkeypatch) -> None:
    """A thread that resolves successfully, followed by a failing dismiss PUT,
    must not have its Checks-tab annotation claim the finding was "NOT
    dismissed/resolved" -- the thread WAS resolved; only the secondary
    review-dismissal step failed. Same request shape as
    test_dismiss_put_failure_surfaces_as_warning_not_silent, with
    GITHUB_ACTIONS set so the annotation path is exercised.
    """
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

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
            return httpx.Response(422, json={"message": "Review is not in a dismissable state"})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "::error::ai-pr-review dismiss-inline:" in result.stderr
    assert "NOT dismissed/resolved" not in result.stderr
    assert "thread was resolved, but a follow-up step" in result.stderr


def test_annotation_silent_when_github_actions_unset(monkeypatch) -> None:
    """The ::error:: annotation must not fire outside GitHub Actions -- e.g.
    local dev runs or non-GHA CI -- even when the underlying command hit an
    API error. Same 401 scenario as
    test_resolve_thread_401_emits_checks_tab_annotation_but_exit_0, with
    GITHUB_ACTIONS left unset.
    """
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert result.exit_code == 0, result.output
    assert "::warning::dismiss-inline: resolve thread" in result.stderr
    assert "::error::" not in result.stderr


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


# ---------------------------------------------------------------------------
# Issue #589: false-positive / wont-fix dismiss like dismiss does when
# posted as a reply to an inline finding.
# ---------------------------------------------------------------------------


def test_false_positive_command_dismisses_review_same_as_dismiss(monkeypatch) -> None:
    """`--command false-positive` must reach the same resolve+dismiss
    behavior as `--command dismiss` -- the CLI's `click.Choice` already
    accepted false-positive/wont-fix before this fix; the bug this pins was
    entirely in the calling workflow's job-trigger `if:` condition and its
    hardcoded `SLASH_COMMAND: dismiss` env var, never in this CLI layer."""
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
    result = runner.invoke(cli, _base_args(55, review_id=41, command="false-positive"))

    assert result.exit_code == 0, result.output
    assert "marked as `false-positive` and resolved the thread" in result.stdout
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]


def test_wont_fix_command_dismisses_review_same_as_dismiss(monkeypatch) -> None:
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
    result = runner.invoke(cli, _base_args(55, review_id=41, command="wont-fix"))

    assert result.exit_code == 0, result.output
    assert "marked as `wont-fix` and resolved the thread" in result.stdout
    assert dismissed == ["https://api.github.com/repos/o/r/pulls/1/reviews/41/dismissals"]


# ---------------------------------------------------------------------------
# Issue #590: --approve-allowed / SLASH_APPROVE_ALLOWED plumbing
# ---------------------------------------------------------------------------


def test_approve_allowed_flag_triggers_pr_approval(monkeypatch) -> None:
    """End-to-end CLI proof that `--approve-allowed true` (as the workflow
    would pass via SLASH_APPROVE_ALLOWED for an OWNER/MEMBER actor) reaches
    the PR-wide approve path and the CLI reports it in the reply and the
    reaction marker."""
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]
    approved: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(
                200, json=[{"id": 41, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}}]
            )
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            approved.append(_json.loads(req.content))
            return httpx.Response(200, json={"id": 999, "state": "APPROVED"})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli, _base_args(55, review_id=41) + ["--approve-allowed", "true"]
    )

    assert result.exit_code == 0, result.output
    assert "approved" in result.stdout
    assert len(approved) == 1


def test_approve_allowed_defaults_false_no_approval(monkeypatch) -> None:
    """Without --approve-allowed (and without SLASH_APPROVE_ALLOWED in the
    environment), the default must be False -- a COLLABORATOR-level actor
    (or any caller that omits the flag) never triggers the PR-wide approve
    escalation, only the ordinary per-review dismiss."""
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

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
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError("must not approve without --approve-allowed")
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41))

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "approved" not in result.stdout


# ---------------------------------------------------------------------------
# "fixed" command: never auto-approves, echoes the commit SHA bare
# ---------------------------------------------------------------------------


def test_fixed_never_approves_even_with_approve_allowed_true(monkeypatch) -> None:
    """Regression guard for the central design decision behind `fixed`: it
    must never trigger the PR-wide auto-approve escalation, even when the
    calling workflow passes --approve-allowed true (as it would for an
    OWNER/MEMBER actor on dismiss/false-positive/wont-fix). A `fixed` claim
    is not a maintainer verdict on the finding -- approval should come from
    the next review cycle re-running against the new commit, not from this
    command. This is the same fixture as
    test_approve_allowed_flag_triggers_pr_approval, with only the command
    changed, to prove the CLI-level override (not just a default) is what's
    doing the work."""
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(
                200, json=[{"id": 41, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}}]
            )
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError("fixed must never submit an APPROVE review")
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli, _base_args(55, review_id=41, command="fixed") + ["--approve-allowed", "true"]
    )

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "the PR has been approved" not in result.stdout
    # The asymmetry (unlike dismiss/false-positive/wont-fix, fixed doesn't
    # auto-approve) is stated explicitly rather than left for the reader to infer.
    assert "not auto-approved" in result.stdout


def test_fixed_echoes_bare_commit_sha_in_reply(monkeypatch) -> None:
    """The optional commit SHA is parsed from --comment-body (not a separate
    flag -- see cli.py's dismiss-inline docstring) and echoed bare (no
    backticks) so GitHub auto-links it to the commit."""
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(200, json=[])
        if req.method == "POST" and url.endswith("/graphql"):
            body = _json.loads(req.content)
            q = body.get("query", "")
            if "resolveReviewThread" in q:
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        _base_args(55, command="fixed")
        + ["--comment-body", "/ai-pr-review fixed abc1234def fixed in the refactor"],
    )

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "abc1234def" in result.stdout
    assert "`abc1234def`" not in result.stdout  # bare, not a code span -- must autolink


def test_approve_allowed_explicit_false_string_env_var_no_approval(monkeypatch) -> None:
    """The workflow passes SLASH_APPROVE_ALLOWED as the literal string
    "false" for a COLLABORATOR-level actor (GitHub Actions' `contains(...)`
    boolean renders to the string "false", not an absent env var) -- this is
    the actual trust-boundary invocation in production, distinct from
    `test_approve_allowed_defaults_false_no_approval`'s omitted-flag case,
    which only proves Click's own default. This pins that Click's BOOL type
    parses that exact string to False rather than truthy-by-presence."""
    our_body = f"[High] leak\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=our_body, comment_db_id=55, review_db_id=41)]

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
            return httpx.Response(200, json={})
        if req.method == "POST" and url.endswith("/pulls/1/reviews"):
            raise AssertionError('must not approve when SLASH_APPROVE_ALLOWED="false"')
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli, _base_args(55, review_id=41), env={"SLASH_APPROVE_ALLOWED": "false"}
    )

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "approved" not in result.stdout


# ---------------------------------------------------------------------------
# Issue #769: this command previously never touched the feedback store at
# all -- the now-removed `feedback-command` workflow job's `slash`
# invocation was the only writer for any inline verdict. `dismiss-inline` is
# now the sole owner of both the reply and the store write on this event
# path.
# ---------------------------------------------------------------------------


def test_feedback_store_write_full_context_from_thread(monkeypatch) -> None:
    """Full context (source, file, rule_id) comes from the thread already
    fetched for resolution -- no extra API call (no separate fetch of the
    parent comment, unlike `context_from_parent_comment`)."""
    f = Finding(
        severity="medium",
        confidence=80,
        finding="unsafe eval",
        source="code-reviewer",
        file="src/foo.py",
        line=12,
    )
    body = _build_inline_comment_body(f, finding_id=None)
    nodes = [
        _inline_thread(
            "T1", resolved=False, body=body, comment_db_id=55, review_db_id=41, path="src/foo.py"
        )
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            gql_body = _json.loads(req.content)
            if "resolveReviewThread" in gql_body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        # No singular parent-comment fetch (that would be
        # context_from_parent_comment's route) -- dismiss_inline_reply
        # already has the thread's first comment in hand.
        if req.method == "GET" and "/pulls/comments/" in url and url.rstrip("/").endswith(str(55)):
            raise AssertionError("must not make a redundant parent-comment fetch for context")
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    appended: list = []
    monkeypatch.setattr(
        "ai_pr_review.feedback.store.make_store",
        lambda config: type("_S", (), {"append": staticmethod(lambda entry: (appended.append(entry), True)[1])})(),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        _base_args(55, review_id=41) + ["--enable-feedback-loop", "1", "--feedback-write-allowed", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert len(appended) == 1
    assert appended[0].source == "code-reviewer"
    assert appended[0].file == "src/foo.py"
    assert appended[0].command == "false-positive"


def test_feedback_write_allowed_false_blocks_store_write(monkeypatch) -> None:
    f = Finding(
        severity="medium", confidence=80, finding="unsafe eval", source="code-reviewer", file="src/foo.py", line=12
    )
    body = _build_inline_comment_body(f, finding_id=None)
    nodes = [_inline_thread("T1", resolved=False, body=body, comment_db_id=55, review_db_id=41, path="src/foo.py")]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            gql_body = _json.loads(req.content)
            if "resolveReviewThread" in gql_body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    def _boom(config):
        raise AssertionError("make_store should not be called when the actor is not trusted")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(55, review_id=41) + ["--enable-feedback-loop", "1"])

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "OWNER or MEMBER" in result.stdout


def test_fixed_inline_never_writes_feedback_store(monkeypatch) -> None:
    f = Finding(
        severity="medium", confidence=80, finding="unsafe eval", source="code-reviewer", file="src/foo.py", line=12
    )
    body = _build_inline_comment_body(f, finding_id=None)
    nodes = [_inline_thread("T1", resolved=False, body=body, comment_db_id=55, review_db_id=41, path="src/foo.py")]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            gql_body = _json.loads(req.content)
            if "resolveReviewThread" in gql_body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    def _boom(config):
        raise AssertionError("`fixed` must never write to the feedback store")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        _base_args(55, review_id=41, command="fixed")
        + ["--enable-feedback-loop", "1", "--feedback-write-allowed", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout


def test_feedback_loop_disabled_takes_precedence_over_write_allowed(monkeypatch) -> None:
    """`--enable-feedback-loop` is a brand-new option on `dismiss-inline`
    (issue #769 -- this command never touched the feedback store before).
    Its CLI wiring in isolation deserves direct coverage: loop-disabled must
    win over write-allowed, matching the BODY path's
    test_body_finding_feedback_loop_disabled_skips_store."""
    f = Finding(
        severity="medium", confidence=80, finding="unsafe eval", source="code-reviewer", file="src/foo.py", line=12
    )
    body = _build_inline_comment_body(f, finding_id=None)
    nodes = [_inline_thread("T1", resolved=False, body=body, comment_db_id=55, review_db_id=41, path="src/foo.py")]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            gql_body = _json.loads(req.content)
            if "resolveReviewThread" in gql_body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    def _boom(config):
        raise AssertionError("make_store should not be called when the feedback loop is disabled")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    # --enable-feedback-loop omitted (defaults False); --feedback-write-allowed
    # explicitly true, to prove loop-disabled is checked first.
    result = runner.invoke(cli, _base_args(55, review_id=41) + ["--feedback-write-allowed", "1"])

    assert result.exit_code == 0, result.output
    assert "resolved the thread" in result.stdout
    assert "feedback loop disabled" in result.stdout
    assert "OWNER or MEMBER" not in result.stdout


def test_feedback_store_failure_reported_honestly_with_no_stable_id(monkeypatch) -> None:
    """The "could not persist" fallback reply's F<n> citation falls back to
    "this finding" only when no F-token could be parsed from the thread body
    -- exercise that fallback text directly, and confirm a store-append
    failure on this path (never covered before) surfaces honestly rather
    than claiming success."""
    # No **[F<n>]** token in this body -- a legacy inline comment predating
    # F-ids on inline findings, or a malformed/hand-crafted one.
    body = f"🔵 **[Medium]** [code-reviewer] unsafe eval\n{INLINE_MARKER}"
    nodes = [_inline_thread("T1", resolved=False, body=body, comment_db_id=55, review_db_id=41, path="src/foo.py")]

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and url.endswith("/reviews/41"):
            return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
        if req.method == "POST" and url.endswith("/graphql"):
            gql_body = _json.loads(req.content)
            if "resolveReviewThread" in gql_body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT" and "/dismissals" in url:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    monkeypatch.setattr(
        "ai_pr_review.feedback.store.make_store",
        lambda config: type("_S", (), {"append": staticmethod(lambda entry: False)})(),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        _base_args(55, review_id=41) + ["--enable-feedback-loop", "1", "--feedback-write-allowed", "1"],
    )

    assert result.exit_code == 0, result.output
    # The store-failure reply replaces result.reply entirely (it's the
    # "honest about what happened" message _persist_verdict returns instead
    # of echoing the thread-resolved confirmation) -- see cli.py's
    # _persist_verdict failure branch.
    assert "this finding" in result.stdout
    assert "could not persist it" in result.stdout


def test_dedup_key_finding_id_from_body_prevents_cross_finding_collision(monkeypatch) -> None:
    """Direct regression test for the bug two independent reviewers of this
    PR flagged: before dismiss_inline threaded a real finding_id through,
    every verdict on this path shared the same
    (command, source, file, rule_id, None) dedup key, so dismissing two
    DIFFERENT findings in the same file/source within the dedup window would
    collapse the second onto the first and silently drop it. This asserts
    the actual store append receives distinct finding_id extras for two
    different F-ids named in two different comment bodies."""
    f = Finding(
        severity="medium", confidence=80, finding="unsafe eval", source="code-reviewer", file="src/foo.py", line=12
    )
    body1 = _build_inline_comment_body(f, finding_id=7)
    body2 = _build_inline_comment_body(f, finding_id=8)

    appended: list = []
    monkeypatch.setattr(
        "ai_pr_review.feedback.store.make_store",
        lambda config: type("_S", (), {"append": staticmethod(lambda entry: (appended.append(entry), True)[1])})(),
    )

    for comment_id, body in ((55, body1), (66, body2)):
        nodes = [
            _inline_thread(
                "T1", resolved=False, body=body, comment_db_id=comment_id, review_db_id=41, path="src/foo.py"
            )
        ]

        def handler(req: httpx.Request, nodes=nodes) -> httpx.Response:
            url = str(req.url)
            if req.method == "GET" and url.endswith("/reviews/41"):
                return httpx.Response(200, json={"id": 41, "state": "CHANGES_REQUESTED"})
            if req.method == "POST" and url.endswith("/graphql"):
                gql_body = _json.loads(req.content)
                if "resolveReviewThread" in gql_body.get("query", ""):
                    return httpx.Response(
                        200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                    )
                return httpx.Response(200, json=_threads_response(nodes))
            if req.method == "PUT" and "/dismissals" in url:
                return httpx.Response(200, json={})
            return httpx.Response(404)

        provider, _ = _make_provider(handler)
        monkeypatch.setattr(vcs_module, "provider_from_env", lambda p=provider: p)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            _base_args(comment_id, review_id=41)
            + ["--enable-feedback-loop", "1", "--feedback-write-allowed", "1"],
        )
        assert result.exit_code == 0, result.output

    assert len(appended) == 2
    assert appended[0].extras.get("finding_id") == 7
    assert appended[1].extras.get("finding_id") == 8
