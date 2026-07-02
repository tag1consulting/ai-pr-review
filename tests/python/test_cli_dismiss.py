"""Tests for the `ai-pr-review dismiss` CLI subcommand (story 13-2).

Wires story 13-1's `dismiss_by_finding_id`/`list_active_body_ids` to the CLI.
Follows the `_make_provider(handler)` HTTP-mocking harness established in
`tests/python/vcs/test_dismiss_github.py`, invoked through Click's CliRunner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from click.testing import CliRunner

import ai_pr_review.vcs as vcs_module
from ai_pr_review.cli import cli
from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[GitHubProvider, _Recorder]:
    rec = _Recorder()

    def _wrap(request: httpx.Request) -> httpx.Response:
        rec.calls.append((request.method, str(request.url), None))
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


def _finding(text: str, source: str, file: str, line: int = 10) -> Finding:
    return Finding(severity="medium", confidence=80, finding=text, source=source, file=file, line=line)


def _base_args(finding_id: int | None, command: str = "dismiss", *, feedback_loop: bool = True) -> list[str]:
    args = ["dismiss", "--actor", "alice", "--command", command, "--pr-number", "5"]
    if finding_id is not None:
        args += ["--finding-id", str(finding_id)]
    if feedback_loop:
        args += ["--enable-feedback-loop", "1"]
    return args


def test_body_finding_writes_feedback_and_echoes_reply(monkeypatch) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
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

    appended: list = []
    monkeypatch.setattr(
        "ai_pr_review.feedback.store.make_store",
        lambda config: type("_S", (), {"append": staticmethod(lambda entry: (appended.append(entry), True)[1])})(),
    )

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3))

    assert result.exit_code == 0, result.output
    assert "F3" in result.output
    assert "suppressed on future review runs" in result.output
    assert len(appended) == 1
    assert appended[0].source == "phpcs"
    assert appended[0].file == "legacy.py"
    assert appended[0].command == "false-positive"


def test_body_finding_feedback_store_failure_is_reported_honestly(monkeypatch) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
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
    monkeypatch.setattr(
        "ai_pr_review.feedback.store.make_store",
        lambda config: type("_S", (), {"append": staticmethod(lambda entry: False)})(),
    )

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3))

    assert result.exit_code == 0, result.output
    assert "could not persist" in result.output
    assert "suppressed on future review runs" not in result.output


def test_body_finding_feedback_loop_disabled_skips_store(monkeypatch) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
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

    def _boom(config):
        raise AssertionError("make_store should not be called when the feedback loop is disabled")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3, feedback_loop=False))

    assert result.exit_code == 0, result.output
    assert "feedback loop disabled" in result.output
    assert "not persisted to learning store" in result.output


def test_reaction_marker_done_for_body_finding(monkeypatch) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
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
    result = runner.invoke(cli, _base_args(3, feedback_loop=False))

    assert "::notice::reaction=done" in result.output


def test_reaction_marker_confused_for_genuine_miss(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(999))

    assert "::notice::reaction=confused" in result.output


def test_inline_finding_does_not_touch_feedback_store(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    def _boom(config):
        raise AssertionError("make_store should not be called for a non-BODY result")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(999))

    assert result.exit_code == 0, result.output
    assert "could not find" in result.output


def test_missing_finding_id_lists_active_ids(monkeypatch) -> None:
    f = _finding("style issue", source="phpcs", file="legacy.py", line=5)
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
    result = runner.invoke(cli, _base_args(None))

    assert result.exit_code == 0, result.output
    assert "F3" in result.output


def test_missing_finding_id_no_active_ids(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(None))

    assert result.exit_code == 0, result.output
    assert "no active body-level findings" in result.output


def test_non_github_provider_fails_closed_with_clear_message() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, _base_args(1), env={"VCS_PROVIDER": "gitlab"})

    assert result.exit_code != 0
    assert "GitHub-only" in result.output
    assert "gitlab" in result.output


def test_missing_required_option_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["dismiss", "--command", "dismiss", "--pr-number", "5"], env={})

    assert result.exit_code != 0
