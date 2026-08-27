"""Tests for ai_pr_review.cli._post_policy_gate_check_run (the
ai-pr-review/policy-gate merge gate — see docs/policy.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from ai_pr_review.cli import _post_policy_gate_check_run
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


@dataclass
class _FakeCheckRunCalls:
    calls: list[dict] = field(default_factory=list)


class _FakeGitHubProvider(GitHubProvider):
    """A GitHubProvider whose post_check_run records calls instead of
    hitting the network — everything else about the class is real, so
    isinstance(provider, GitHubProvider) checks in the code under test
    still pass.
    """

    def __init__(self) -> None:
        client = RecordingClient(
            http=None,  # never used — post_check_run is overridden below
            recorder=TapeRecorder(record_dir=None),
            retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False),
        )
        super().__init__(config=GitHubConfig(owner="o", repo="r", pr_number=1, token="t"), client=client)
        self.recorded = _FakeCheckRunCalls()

    def post_check_run(
        self, head_sha: str, name: str, conclusion: str, title: str, summary: str
    ) -> bool:
        self.recorded.calls.append(
            {
                "head_sha": head_sha,
                "name": name,
                "conclusion": conclusion,
                "title": title,
                "summary": summary,
            }
        )
        return True


def _fake_runtime(*, policy_gate_required, policy_gate_satisfied, provider, head_sha="abc1234"):
    return SimpleNamespace(
        policy_gate_required=policy_gate_required,
        policy_gate_satisfied=policy_gate_satisfied,
        provider=provider,
        head_sha=head_sha,
    )


def test_no_requirement_is_a_noop() -> None:
    provider = _FakeGitHubProvider()
    runtime = _fake_runtime(policy_gate_required=None, policy_gate_satisfied=True, provider=provider)
    _post_policy_gate_check_run(runtime)  # type: ignore[arg-type]
    assert provider.recorded.calls == []


def test_satisfied_posts_success() -> None:
    provider = _FakeGitHubProvider()
    runtime = _fake_runtime(
        policy_gate_required="deep", policy_gate_satisfied=True, provider=provider
    )
    _post_policy_gate_check_run(runtime)  # type: ignore[arg-type]
    assert len(provider.recorded.calls) == 1
    call = provider.recorded.calls[0]
    assert call["name"] == "ai-pr-review/policy-gate"
    assert call["conclusion"] == "success"
    assert call["head_sha"] == "abc1234"
    assert "deep" in call["title"]


def test_unsatisfied_posts_action_required_not_neutral() -> None:
    # "neutral" satisfies GitHub's required-status-check pass set
    # (success/neutral/skipped) the same as "success" does, which would
    # silently defeat the merge gate (#688). "action_required" is excluded
    # from that set, so it still blocks merge.
    provider = _FakeGitHubProvider()
    runtime = _fake_runtime(
        policy_gate_required="deep", policy_gate_satisfied=False, provider=provider
    )
    _post_policy_gate_check_run(runtime)  # type: ignore[arg-type]
    call = provider.recorded.calls[0]
    assert call["conclusion"] == "action_required"
    assert "review-full" in call["summary"]


def test_non_github_provider_is_a_noop() -> None:
    fake_non_github_provider = SimpleNamespace()
    runtime = _fake_runtime(
        policy_gate_required="deep", policy_gate_satisfied=False, provider=fake_non_github_provider
    )
    # Must not raise even though the fake provider has no post_check_run method.
    _post_policy_gate_check_run(runtime)  # type: ignore[arg-type]
