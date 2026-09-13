"""Tests for the CLI-adjacent pure helpers moved into ai_pr_review.slash.dismiss
by issue #825 (out of ai_pr_review/cli.py).

These back cli.py's `_build_github_provider_or_exit`/`_build_github_provider_or_none`,
`dismiss`'s "no finding ID" reply, `_emit_dismiss_failure_annotation`, and
`feedback-context`'s context resolution -- exercised indirectly and extensively
by the CLI-level tests (test_cli_dismiss.py, test_cli_dismiss_inline.py,
test_cli_feedback_context.py) via Click's CliRunner, but covered here directly
since the logic itself now lives in this module.
"""

from __future__ import annotations

import ai_pr_review.vcs as vcs_module
from ai_pr_review.slash.dismiss import (
    FeedbackContext,
    GitHubProviderError,
    dismiss_failure_annotation,
    no_finding_id_reply,
    resolve_feedback_context,
    resolve_github_provider,
)
from ai_pr_review.vcs import ProviderConfigError
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


def _dummy_provider() -> GitHubProvider:
    client = RecordingClient(
        http=None,  # type: ignore[arg-type]
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=1, token="t")
    return GitHubProvider(config=config, client=client)


# ---------------------------------------------------------------------------
# resolve_github_provider
# ---------------------------------------------------------------------------


def test_resolve_github_provider_non_github_vcs(monkeypatch) -> None:
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    result = resolve_github_provider("dismiss")
    assert isinstance(result, GitHubProviderError)
    assert "GitHub-only" in result.message
    assert "dismiss" in result.message


def test_resolve_github_provider_config_error(monkeypatch) -> None:
    monkeypatch.delenv("VCS_PROVIDER", raising=False)

    def _boom() -> GitHubProvider:
        raise ProviderConfigError("missing GH_TOKEN")

    monkeypatch.setattr(vcs_module, "provider_from_env", _boom)
    result = resolve_github_provider("feedback-context")
    assert isinstance(result, GitHubProviderError)
    assert "missing GH_TOKEN" in result.message
    assert result.message.startswith("feedback-context: ")


def test_resolve_github_provider_success(monkeypatch) -> None:
    monkeypatch.delenv("VCS_PROVIDER", raising=False)
    provider = _dummy_provider()
    monkeypatch.setattr(vcs_module, "provider_from_env", lambda: provider)
    result = resolve_github_provider("dismiss")
    assert result is provider


# ---------------------------------------------------------------------------
# no_finding_id_reply
# ---------------------------------------------------------------------------


def test_no_finding_id_reply_with_active_ids() -> None:
    reply = no_finding_id_reply("alice", "dismiss", [2, 5, 9])
    assert reply == (
        "@alice please specify a finding ID, e.g. `/ai-pr-review dismiss F2`. "
        "Active findings: F2, F5, F9."
    )


def test_no_finding_id_reply_no_active_ids() -> None:
    reply = no_finding_id_reply("alice", "wont-fix", [])
    assert reply == "@alice there are no active body-level findings to wont-fix."


# ---------------------------------------------------------------------------
# dismiss_failure_annotation
# ---------------------------------------------------------------------------


def test_dismiss_failure_annotation_none_outside_actions(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert dismiss_failure_annotation("dismiss", ("boom",)) is None


def test_dismiss_failure_annotation_none_when_no_errors(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert dismiss_failure_annotation("dismiss", ()) is None


def test_dismiss_failure_annotation_not_resolved(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    line = dismiss_failure_annotation("dismiss", ("http 500",), thread_resolved=False)
    assert line is not None
    assert line.startswith("::error::ai-pr-review dismiss:")
    assert "likely NOT dismissed/resolved" in line


def test_dismiss_failure_annotation_thread_resolved(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    line = dismiss_failure_annotation("dismiss-inline", ("http 500", "http 502"), thread_resolved=True)
    assert line is not None
    assert "2 API error(s)" in line
    assert "follow-up step" in line


# ---------------------------------------------------------------------------
# resolve_feedback_context
# ---------------------------------------------------------------------------


def test_resolve_feedback_context_none_provider() -> None:
    context = resolve_feedback_context(
        None, is_review_comment=False, parent_comment_id=0, comment_body="/ai-pr-review dismiss F1"
    )
    assert context == FeedbackContext()


def test_resolve_feedback_context_no_fid_token() -> None:
    context = resolve_feedback_context(
        _dummy_provider(),
        is_review_comment=False,
        parent_comment_id=0,
        comment_body="/ai-pr-review dismiss not-an-id",
    )
    assert context == FeedbackContext()
