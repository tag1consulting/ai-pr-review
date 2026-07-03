"""Tests for the `ai-pr-review feedback-context` and `resolve-thread` CLI
subcommands (story 13-4).

Wires `context_from_parent_comment` / `context_from_body_finding_id` /
`resolve_only` to the CLI. Both commands back best-effort workflow steps in
`feedback-command` and must never fail the step on a provider construction
or lookup error — unlike `dismiss`/`dismiss-inline`, they degrade to a
warning and exit 0. Follows the `_make_provider(handler)` HTTP-mocking
harness established in `tests/python/vcs/test_dismiss_github.py`.
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
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider, _build_inline_comment_body
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


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


def _finding(text: str, source: str = "code-reviewer", file: str = "app.py", line: int = 10) -> Finding:
    return Finding(severity="medium", confidence=80, finding=text, source=source, file=file, line=line)


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
                    "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# feedback-context
# ---------------------------------------------------------------------------


def test_feedback_context_review_comment_happy_path(monkeypatch) -> None:
    f = _finding("SQL injection", source="security-reviewer", file="db.py")
    body = _build_inline_comment_body(f, finding_id=3)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/pulls/comments/123" in str(req.url):
            return httpx.Response(200, json={"user": {"login": "github-actions[bot]"}, "path": "db.py", "body": body})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "feedback-context",
            "--pr-number",
            "5",
            "--is-review-comment",
            "true",
            "--parent-comment-id",
            "123",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "source=security-reviewer" in result.stdout
    assert "file=db.py" in result.stdout
    assert "context_missing_reason" not in result.stdout


def test_feedback_context_file_newline_cannot_forge_github_output_line(monkeypatch) -> None:
    """A newline embedded in the REST comment's `path` field (the one
    context field not bounded to a single line by regex before this CLI
    layer) must not inject a forged key=value line into $GITHUB_OUTPUT."""
    f = _finding("SQL injection", source="security-reviewer", file="db.py")
    body = _build_inline_comment_body(f, finding_id=3)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/pulls/comments/123" in str(req.url):
            return httpx.Response(
                200,
                json={
                    "user": {"login": "github-actions[bot]"},
                    "path": "db.py\nrule_id=forged",
                    "body": body,
                },
            )
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feedback-context", "--pr-number", "5", "--is-review-comment", "true", "--parent-comment-id", "123"],
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "rule_id=forged" not in lines
    assert any(line.startswith("file=db.py") for line in lines)


def test_feedback_context_review_comment_missing_reason_exported(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feedback-context", "--pr-number", "5", "--is-review-comment", "true", "--parent-comment-id", "123"],
    )

    assert result.exit_code == 0, result.output
    assert "context_missing_reason=" in result.stdout
    assert "could not fetch parent comment" in result.stderr


def test_feedback_context_issue_comment_body_finding(monkeypatch) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py")
    bullet = format_body_finding(f, finding_id=3)
    review_body = "### Findings not attached to specific lines\n\n" + bullet + "\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 1, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "feedback-context",
            "--pr-number",
            "5",
            "--is-review-comment",
            "false",
            "--comment-body",
            "/ai-pr-review false-positive F3 not a real bug",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "source=phpcs" in result.stdout
    assert "file=legacy.py" in result.stdout
    # issue_comment path never exports context_missing_reason, even on a hit.
    assert "context_missing_reason" not in result.stdout


def test_feedback_context_issue_comment_no_fid_token_is_silent(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the API with no F<n> token in the comment")

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "feedback-context",
            "--pr-number",
            "5",
            "--is-review-comment",
            "false",
            "--comment-body",
            "/ai-pr-review feedback this whole approach seems fragile",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert result.stderr == ""


def test_feedback_context_issue_comment_inline_finding_emits_notice_not_warning(monkeypatch) -> None:
    id_map_body = 'Some review.\n<!-- ai-pr-review-id-map: {"x|y.py|1|aaaaaaaaaaaa": 4} -->'

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[{"id": 1, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": id_map_body}],
            )
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "feedback-context",
            "--pr-number",
            "5",
            "--is-review-comment",
            "false",
            "--comment-body",
            "/ai-pr-review false-positive F4 wrong bucket",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "::notice::" in result.stderr
    assert "inline finding" in result.stderr


def test_feedback_context_non_github_provider_degrades_gracefully() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["feedback-context", "--pr-number", "5", "--is-review-comment", "true", "--parent-comment-id", "123"],
        env={"VCS_PROVIDER": "gitlab"},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "GitHub-only" in result.stderr


# ---------------------------------------------------------------------------
# resolve-thread
# ---------------------------------------------------------------------------


def test_resolve_thread_resolves_without_dismissing(monkeypatch) -> None:
    nodes = [_inline_thread("T1", resolved=False, body="our finding", comment_db_id=77, review_db_id=41)]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            body = _json.loads(req.content)
            if "resolveReviewThread" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}})
            return httpx.Response(200, json=_threads_response(nodes))
        if req.method == "PUT":
            raise AssertionError("resolve-thread must never issue a dismiss PUT")
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, ["resolve-thread", "--parent-comment-id", "77", "--pr-number", "5"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_resolve_thread_failure_warns_but_exits_zero(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and str(req.url).endswith("/graphql"):
            return httpx.Response(200, json=_threads_response([]))
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, ["resolve-thread", "--parent-comment-id", "999", "--pr-number", "5"])

    assert result.exit_code == 0, result.output
    assert "could not locate" in result.stderr


def test_resolve_thread_non_github_provider_degrades_gracefully() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["resolve-thread", "--parent-comment-id", "77", "--pr-number", "5"],
        env={"VCS_PROVIDER": "bitbucket"},
    )

    assert result.exit_code == 0, result.output
    assert "GitHub-only" in result.stderr


def test_resolve_thread_missing_required_option_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["resolve-thread", "--pr-number", "5"], env={})

    assert result.exit_code != 0
