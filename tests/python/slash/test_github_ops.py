"""Tests for `ai_pr_review.slash.github_ops` -- currently just `persist_verdict`'s
`_DismissConfig` construction (issue #906 follow-up).
"""

from __future__ import annotations

import ai_pr_review.feedback.store as feedback_store
from ai_pr_review.slash.github_ops import DismissResult, persist_verdict


def _eligible_result() -> DismissResult:
    return DismissResult(
        reply="@alice marked **F1** as `false-positive`.",
        feedback_source="pylint",
        feedback_file="app.py",
        feedback_rule_id="",
        feedback_finding_id=1,
        feedback_eligible=True,
        acted=True,
    )


def test_persist_verdict_honors_feedback_branch_env(monkeypatch) -> None:
    """Before #906's follow-up fix, `_DismissConfig` hard-coded
    `vcs_provider = "github"` with no `feedback_branch`/retention attributes,
    so `make_store()`'s `getattr(config, "feedback_branch", default)` always
    fell back to the default branch regardless of `AI_FEEDBACK_BRANCH` --
    silently ignoring the workflow's own configuration on the dismiss path.
    """
    monkeypatch.setenv("AI_FEEDBACK_BRANCH", "custom-feedback-branch")
    monkeypatch.setenv("AI_FEEDBACK_RETENTION_COUNT", "42")
    monkeypatch.setenv("AI_FEEDBACK_RETENTION_AGE_DAYS", "7")

    captured: dict[str, object] = {}

    class _FakeStore:
        def append(self, entry: object) -> bool:
            return True

    def _fake_make_store(config: object) -> _FakeStore:
        captured["branch"] = getattr(config, "feedback_branch", None)
        captured["retention_count"] = getattr(config, "feedback_retention_count", None)
        captured["retention_age_days"] = getattr(config, "feedback_retention_age_days", None)
        return _FakeStore()

    monkeypatch.setattr(feedback_store, "make_store", _fake_make_store)

    reply = persist_verdict(
        _eligible_result(),
        actor="alice",
        command_name="false-positive",
        finding_id=1,
        comment_body="/ai-pr-review false-positive F1",
        parsed_command=None,
        enable_feedback_loop=True,
        feedback_write_allowed=True,
    )

    assert captured == {
        "branch": "custom-feedback-branch",
        "retention_count": 42,
        "retention_age_days": 7,
    }
    assert reply == _eligible_result().reply


def test_persist_verdict_falls_back_to_defaults_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("AI_FEEDBACK_BRANCH", raising=False)
    monkeypatch.delenv("AI_FEEDBACK_RETENTION_COUNT", raising=False)
    monkeypatch.delenv("AI_FEEDBACK_RETENTION_AGE_DAYS", raising=False)

    captured: dict[str, object] = {}

    class _FakeStore:
        def append(self, entry: object) -> bool:
            return True

    def _fake_make_store(config: object) -> _FakeStore:
        captured["branch"] = getattr(config, "feedback_branch", None)
        captured["retention_count"] = getattr(config, "feedback_retention_count", None)
        captured["retention_age_days"] = getattr(config, "feedback_retention_age_days", None)
        return _FakeStore()

    monkeypatch.setattr(feedback_store, "make_store", _fake_make_store)

    persist_verdict(
        _eligible_result(),
        actor="alice",
        command_name="wont-fix",
        finding_id=1,
        comment_body="/ai-pr-review wont-fix",
        parsed_command=None,
        enable_feedback_loop=True,
        feedback_write_allowed=True,
    )

    assert captured == {
        "branch": "ai-pr-review-bot",
        "retention_count": 500,
        "retention_age_days": 365,
    }


def test_persist_verdict_falls_back_on_malformed_retention_env(monkeypatch) -> None:
    """`_DismissConfig` calls `_int_env` directly (not through `ReviewConfig.from_env()`);
    this exercises that call site's own malformed-value path rather than relying on
    `test_config.py`'s generic coverage of `_int_env` to stand in for it.
    """
    monkeypatch.setenv("AI_FEEDBACK_RETENTION_COUNT", "not-a-number")
    monkeypatch.delenv("AI_FEEDBACK_BRANCH", raising=False)
    monkeypatch.delenv("AI_FEEDBACK_RETENTION_AGE_DAYS", raising=False)

    captured: dict[str, object] = {}

    class _FakeStore:
        def append(self, entry: object) -> bool:
            return True

    def _fake_make_store(config: object) -> _FakeStore:
        captured["retention_count"] = getattr(config, "feedback_retention_count", None)
        return _FakeStore()

    monkeypatch.setattr(feedback_store, "make_store", _fake_make_store)

    persist_verdict(
        _eligible_result(),
        actor="alice",
        command_name="false-positive",
        finding_id=1,
        comment_body="/ai-pr-review false-positive F1",
        parsed_command=None,
        enable_feedback_loop=True,
        feedback_write_allowed=True,
    )

    assert captured == {"retention_count": 500}
