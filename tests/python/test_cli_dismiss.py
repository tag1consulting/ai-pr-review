"""Tests for the `ai-pr-review dismiss` CLI subcommand (story 13-2).

Wires story 13-1's `dismiss_by_finding_id`/`list_active_body_ids` to the CLI.
Follows the `_make_provider(handler)` HTTP-mocking harness established in
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
from ai_pr_review.vcs._body import format_body_finding
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider, _build_inline_comment_body
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import build_id_map_marker


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


def _base_args(
    finding_id: int | None,
    command: str = "dismiss",
    *,
    feedback_loop: bool = True,
    feedback_write_allowed: bool = True,
) -> list[str]:
    args = ["dismiss", "--actor", "alice", "--command", command, "--pr-number", "5"]
    if finding_id is not None:
        args += ["--finding-id", str(finding_id)]
    if feedback_loop:
        args += ["--enable-feedback-loop", "1"]
    if feedback_write_allowed:
        args += ["--feedback-write-allowed", "1"]
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
    assert "F3" in result.stdout
    assert "suppressed on future review runs" in result.stdout
    assert len(appended) == 1
    assert appended[0].source == "phpcs"
    assert appended[0].file == "legacy.py"
    assert appended[0].command == "false-positive"


def test_body_finding_feedback_write_not_allowed_blocks_store(monkeypatch) -> None:
    """`--feedback-write-allowed=False` on the BODY (top-level `dismiss`)
    path -- this is the job `dismiss-body-finding` gates with
    SLASH_FEEDBACK_WRITE_ALLOWED (issue #769's closed COLLABORATOR gap), and
    it was previously untested here: every other test in this file uses
    `_base_args`'s default of `feedback_write_allowed=True`. Mirrors
    `test_feedback_write_allowed_false_blocks_store_write` in
    test_cli_dismiss_inline.py, which covers the same gate on the INLINE
    path."""
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
        raise AssertionError("make_store should not be called when the actor is not trusted")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3, feedback_write_allowed=False))

    assert result.exit_code == 0, result.output
    assert "F3" in result.stdout
    assert "OWNER or MEMBER" in result.stdout
    # Distinguish from the "feedback loop disabled" message -- this reply
    # must say the actor isn't trusted, not that the feature is off.
    assert "feedback loop disabled" not in result.stdout


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
    assert "could not persist" in result.stdout
    assert "suppressed on future review runs" not in result.stdout


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
    assert "feedback loop disabled" in result.stdout
    assert "not persisted to learning store" in result.stdout


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

    # The reaction marker must be on stderr only: the workflow wrapper base64s
    # `reply` from stdout (`reply=$(ai-pr-review dismiss 2>/tmp/dismiss-stderr)`)
    # and reads the marker from the redirected stderr file separately. A marker
    # that leaks onto stdout would corrupt the posted reply comment.
    assert "::notice::reaction=done" in result.stderr
    assert "::notice::reaction=done" not in result.stdout
    assert "F3" in result.stdout


def test_reaction_marker_confused_for_genuine_miss(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(999))

    assert "::notice::reaction=confused" in result.stderr
    assert "::notice::reaction=confused" not in result.stdout
    assert "could not find" in result.stdout


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
    assert "could not find" in result.stdout


def test_top_level_inline_finding_writes_full_context_feedback(monkeypatch) -> None:
    """This PR's headline new capability, end-to-end through the actual
    `dismiss` CLI command (previously covered only at the
    `dismiss_by_finding_id` unit level in test_dismiss_verdicts.py, and at
    the CLI level only for `dismiss-inline`): a top-level `/ai-pr-review
    dismiss F<n>` naming an F-id that classifies as an INLINE finding (an
    id-map entry with no matching body bullet) resolves its thread AND
    writes a full-context (source/file/rule_id) feedback-store entry -- not
    the empty-context entry the now-removed `feedback-command` path used to
    write for this exact case."""
    finding_id = 4
    # Review body carries the id-map marker only (no body bullet for F4), so
    # classify_finding resolves it as INLINE -- see
    # test_inline_finding_classifies_via_id_map_not_bullet in
    # tests/python/slash/test_dismiss.py for the minimal fixture this mirrors.
    id_map = {"code-reviewer|src/foo.py|12|abc123456789": finding_id}
    review_body = "Some review body.\n" + build_id_map_marker(id_map)

    f = _finding("unsafe eval", source="code-reviewer", file="src/foo.py", line=12)
    thread_body = _build_inline_comment_body(f, finding_id=finding_id)
    thread_node = {
        "id": "T1",
        "isResolved": False,
        "path": "src/foo.py",
        "comments": {
            "nodes": [
                {
                    "body": thread_body,
                    "author": {"login": "github-actions[bot]"},
                    "pullRequestReview": None,
                }
            ]
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.method == "GET" and "/reviews" in url:
            return httpx.Response(
                200,
                json=[{"id": 1, "state": "COMMENTED", "user": {"login": "github-actions[bot]"}, "body": review_body}],
            )
        if req.method == "POST" and url.endswith("/graphql"):
            gql_body = _json.loads(req.content)
            if "resolveReviewThread" in gql_body.get("query", ""):
                return httpx.Response(
                    200, json={"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [thread_node],
                                }
                            }
                        }
                    }
                },
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
    result = runner.invoke(cli, _base_args(finding_id))

    assert result.exit_code == 0, result.output
    assert f"F{finding_id}" in result.stdout
    assert "resolved the thread" in result.stdout
    assert len(appended) == 1
    assert appended[0].source == "code-reviewer"
    assert appended[0].file == "src/foo.py"
    assert appended[0].extras.get("finding_id") == finding_id


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
    assert "F3" in result.stdout


def test_missing_finding_id_excludes_id_moved_inline_in_newer_review(monkeypatch) -> None:
    """Regression test for the CLI wiring, not just the pure classifier:
    `dismiss`'s missing-finding-id branch (cli.py) must route
    `provider.list_bot_reviews()` through `bodies_newest_first` before
    calling `list_active_body_ids` -- with only one mocked review (as every
    other test in this file uses), ordering is a no-op and this wiring
    could be silently dropped without failing any test. Here F9 is a body
    bullet in the older review (id 1) but only an id-map entry in the
    newer review (id 2, F9's line moved into the diff and became inline);
    F5 is a body bullet in both. The active-ID hint must offer F5 but not
    F9 -- offering F9 would steer a user into a `/ai-pr-review dismiss F9`
    that (per the classify_finding fix) resolves as INLINE and can't be
    acted on via the body-only path this hint advertises."""
    f9 = _finding("secret", source="trufflehog", file="hubspotForm.js", line=62)
    f5 = _finding("style issue", source="phpcs", file="legacy.py", line=5)
    older_bullet_f9 = format_body_finding(f9, finding_id=9)
    older_bullet_f5 = format_body_finding(f5, finding_id=5)
    older_body = (
        "### Findings not attached to specific lines\n\n"
        + older_bullet_f9 + "\n" + older_bullet_f5 + "\n"
    )
    newer_bullet_f5 = format_body_finding(f5, finding_id=5)
    newer_body = (
        "### Findings not attached to specific lines\n\n" + newer_bullet_f5 + "\n"
        + build_id_map_marker({"trufflehog|hubspotForm.js|62|abc123456789": 9})
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "state": "APPROVED", "user": {"login": "github-actions[bot]"}, "body": older_body},
                    {"id": 2, "state": "CHANGES_REQUESTED", "user": {"login": "github-actions[bot]"}, "body": newer_body},
                ],
            )
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(None))

    assert result.exit_code == 0, result.output
    assert "F5" in result.stdout
    assert "F9" not in result.stdout


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
    assert "no active body-level findings" in result.stdout


def test_list_reviews_401_emits_checks_tab_annotation_but_exit_0(monkeypatch) -> None:
    """#611: a 401 listing bot reviews (the API call backing F<n> classification)
    must not be a silent green success. dismiss_by_finding_id classifies as
    UNKNOWN with provider._errors populated, which the "not found" reply
    already distinguishes with "due to an API error" -- this test additionally
    covers that the CLI now also emits a Checks-tab ::error:: annotation for
    that case, on top of the existing ::warning:: line, while still exiting 0
    so the reply keeps posting via the workflow's fallback token.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3, feedback_loop=False))

    assert result.exit_code == 0, result.output
    assert "due to an API error" in result.stdout
    assert "::warning::dismiss: list reviews" in result.stderr
    assert "401" in result.stderr
    assert "::error::ai-pr-review dismiss:" in result.stderr
    assert "NOT dismissed/resolved" in result.stderr


def test_list_reviews_401_annotation_silent_when_github_actions_unset(monkeypatch) -> None:
    """The ::error:: annotation must not fire outside GitHub Actions -- e.g.
    local dev runs or non-GHA CI -- even when the underlying command hit an
    API error. Same 401 scenario as
    test_list_reviews_401_emits_checks_tab_annotation_but_exit_0, with
    GITHUB_ACTIONS left unset.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/reviews" in str(req.url):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(404)

    provider, _ = _make_provider(handler)
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3, feedback_loop=False))

    assert result.exit_code == 0, result.output
    assert "::warning::dismiss: list reviews" in result.stderr
    assert "::error::" not in result.stderr


def test_non_github_provider_fails_closed_with_clear_message() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, _base_args(1), env={"VCS_PROVIDER": "gitlab"})

    assert result.exit_code != 0
    assert "GitHub-only" in result.stderr
    assert "gitlab" in result.stderr


def test_missing_required_option_fails() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["dismiss", "--command", "dismiss", "--pr-number", "5"], env={})

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# "fixed" command on a BODY-level finding
# ---------------------------------------------------------------------------


def test_fixed_body_finding_never_writes_feedback_store(monkeypatch) -> None:
    """Central regression guard: unlike dismiss/false-positive/wont-fix, a
    `fixed` verdict on a BODY-level finding must never reach the feedback
    store, even with --enable-feedback-loop set. Recording it would be read
    by the governance prompt as a suppression signal for a finding the
    governance prompt has no rule for -- see
    SlashCommand.is_feedback_command's docstring."""
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
        raise AssertionError("make_store must not be called for a `fixed` verdict")

    monkeypatch.setattr("ai_pr_review.feedback.store.make_store", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, _base_args(3, command="fixed", feedback_loop=True))

    assert result.exit_code == 0, result.output
    assert "F3" in result.stdout
    assert "fixed" in result.stdout
    # Must not claim a suppression verdict was recorded -- that's false for "fixed".
    assert "suppressed on future review runs" not in result.stdout
    # Regression guard for the bug DismissResult.acted was introduced to fix
    # (issue #769): a successful BODY `fixed` sets none of feedback_source/
    # feedback_file/thread_resolved/review_dismissed/pr_approved (no thread
    # exists for a body-level finding), so the old acted-derived-from-those-
    # fields expression reacted "confused" to a command that fully succeeded.
    assert "::notice::reaction=done" in result.stderr
    assert "::notice::reaction=confused" not in result.stderr


def test_fixed_body_finding_echoes_bare_sha(monkeypatch) -> None:
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
    args = _base_args(3, command="fixed", feedback_loop=False) + [
        "--comment-body", "/ai-pr-review fixed F3 abc1234def",
    ]
    result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert "abc1234def" in result.stdout
    assert "`abc1234def`" not in result.stdout  # bare, not a code span -- must autolink
