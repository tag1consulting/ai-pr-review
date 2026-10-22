"""Story 17-10: a fork run sends no PR body, commit message, open-issue title or branch name.

A fork run is a run with a ForkProfile. The tests drive the real runtime, the real CLI preflight and
the real review orchestration with a recording fake for the LLM call. They check that each dropped
channel is absent from EVERY request (system prompt, system prefix, cache blocks, user message) of a
fork run, that the two preflight agents are not called, and that a normal run sends all of it.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from ai_pr_review.config import ReviewConfig
from ai_pr_review.diff.compute import DiffResult
from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest, LLMResponse
from ai_pr_review.review.fork_gate import ForkProfile
from ai_pr_review.review.preflight import (
    FORK_SUMMARY_NOTICE,
    should_run_issue_linker,
    should_run_summarizer,
)
from ai_pr_review.vcs.protocol import FindingsResult, StaleResult, SummaryResult

PROFILE = ForkProfile("mallory/r", "mallory", "NONE")
TITLE = "CANARY-TITLE add a widget"
BODY = "CANARY-BODY this pull request is described here"
COMMIT = "CANARY-COMMIT message of the fork commit"
BRANCH = "CANARY-BRANCH-fork-feature"
ISSUE = "CANARY-ISSUE an open issue title"
CHANNELS = {"body": BODY, "commit": COMMIT, "branch": BRANCH, "issue": ISSUE}


def _config(**kwargs: object) -> ReviewConfig:
    base: dict[str, object] = {
        "base_ref": "main", "head_sha": "abc1234", "max_diff_lines": 5000, "review_mode": "full",
        "vcs_provider": "github", "provider": "anthropic", "pr_number": "42",
        "confidence_threshold": 75, "review_target": "pr", "engine": "python", "parallel": True,
        "max_inline": 25, "max_tokens_per_agent": 8192, "enable_suggestions": True,
        "temperature": 0.3, "llm_prompt_caching": "auto", "force_full_diff": False,
        "model_standard": "claude-sonnet-4-6", "model_premium": "claude-opus-4-7",
        "github_repository": "o/r", "head_ref": BRANCH,
    }
    base.update(kwargs)
    return ReviewConfig.model_validate(base)


def _diff(is_incremental: bool = False) -> DiffResult:
    return DiffResult(
        diff_text="--- a/src/main.py\n+++ b/src/main.py\n@@ -1,1 +1,2 @@\n+new line\n",
        changed_files=["src/main.py"], diff_stat="1 file changed", diff_label="full (main..abc1234)",
        base="main", head="abc1234", is_incremental=is_incremental,
    )


class _Provider:
    """A VcsProvider fake. It records what it posts."""

    def __init__(self) -> None:
        self.summaries: list[str] = []

    def get_last_reviewed_sha(self) -> str | None:
        return None

    def get_summary_body(self) -> str | None:
        return None

    def get_pr_description(self) -> tuple[str, str] | None:
        return TITLE, BODY

    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        self.summaries.append(summary_body)
        return SummaryResult(comment_id=1, created=True, updated=False)

    def post_findings(self, findings: Sequence[Finding], diff: Any, **kwargs: Any) -> FindingsResult:
        return FindingsResult(review_id=1, inline_posted=0, body_findings=0, event="COMMENT")

    def resolve_stale(self, **kwargs: Any) -> StaleResult:
        return StaleResult(threads_resolved=0, reviews_dismissed=0)

    def advance_sha_watermark(self, new_sha: str) -> bool:
        return True

    def post_skip_comment(self, reason: str) -> SummaryResult:
        return SummaryResult(comment_id=None, created=False, updated=False)


def _fake_subprocess(commands: list[Sequence[str]]) -> SimpleNamespace:
    """Answer the three commands the preflight agents run with the canary values."""

    def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        if cmd[0] == "gh":
            out = json.dumps([{"number": 7, "title": ISSUE, "labels": []}])
        elif "rev-parse" in cmd:
            out = BRANCH
        elif "log" in cmd:
            out = f"abc1234 {COMMIT}"
        else:
            out = ""
        return subprocess.CompletedProcess(list(cmd), 0, stdout=out, stderr="")

    return SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=subprocess.CompletedProcess)


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fork: bool, incremental: bool = False,
    commands: list[Sequence[str]] | None = None, **cfg: object,
) -> tuple[list[LLMRequest], _Provider]:
    """Run the whole review once and return every LLM request it made."""
    from ai_pr_review.cli import _run_review_async

    monkeypatch.setenv("AI_PR_REVIEW_DIFF_FILE", str(tmp_path / "diff.txt"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    requests: list[LLMRequest] = []

    async def fake_llm(req: LLMRequest, provider: str) -> LLMResponse:
        requests.append(req)
        return LLMResponse(text="Looks fine.\n\n```json-findings\n[]\n```\n", input_tokens=10, output_tokens=5)

    provider = _Provider()
    config = _config(**cfg)

    with (
        patch("ai_pr_review.diff.compute.compute_diff", return_value=_diff(incremental)),
        patch("ai_pr_review.review.runtime.provider_from_env", return_value=provider),
        patch("ai_pr_review.review.runtime._apply_fork_gate", side_effect=lambda c, p: (c, None, PROFILE if fork else None)),
        patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
        patch("ai_pr_review.llm.client.call_llm", new=fake_llm),
        patch("ai_pr_review.review.preflight.subprocess", _fake_subprocess([] if commands is None else commands)),
    ):
        anyio.run(_run_review_async, config)
    return requests, provider


def _everything(req: LLMRequest) -> str:
    """Every string a request carries: system prompt, prefix, cache blocks and user message."""
    return json.dumps(dataclasses.asdict(req), default=str)


def _names(requests: list[LLMRequest]) -> set[str]:
    text = "\n".join(_everything(r) for r in requests)
    return {k for k, v in CHANNELS.items() if v in text}


# --- a normal run sends every channel (so the absence below means something) ---


def test_a_normal_run_sends_body_commit_branch_and_issue_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _run(tmp_path, monkeypatch, fork=False)
    assert _names(requests) == set(CHANNELS)
    assert any(TITLE in _everything(r) for r in requests)


# --- a fork run sends none of them, in any request ---


def test_a_fork_run_sends_none_of_the_dropped_channels_in_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _run(tmp_path, monkeypatch, fork=True)
    assert requests, "the review agents must still run"
    assert _names(requests) == set()


def test_a_fork_run_keeps_the_pr_title_in_the_agent_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _run(tmp_path, monkeypatch, fork=True)
    assert any(TITLE in _everything(r) for r in requests)


@pytest.mark.parametrize("channel", ["body", "commit", "branch", "issue"])
def test_each_dropped_channel_is_present_in_a_normal_run_and_absent_in_a_fork_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str,
) -> None:
    normal, _ = _run(tmp_path, monkeypatch, fork=False)
    fork, _ = _run(tmp_path, monkeypatch, fork=True)
    assert channel in _names(normal)
    assert channel not in _names(fork)


def test_a_fork_run_calls_neither_the_summarizer_nor_the_issue_linker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    summarizer, linker = AsyncMock(return_value=""), AsyncMock(return_value="")
    with (
        patch("ai_pr_review.cli._run_summarizer", new=summarizer),
        patch("ai_pr_review.cli._run_issue_linker", new=linker),
    ):
        _run(tmp_path, monkeypatch, fork=True)
    assert summarizer.await_count == 0 and linker.await_count == 0
    normal_summarizer, normal_linker = AsyncMock(return_value=""), AsyncMock(return_value="")
    with (
        patch("ai_pr_review.cli._run_summarizer", new=normal_summarizer),
        patch("ai_pr_review.cli._run_issue_linker", new=normal_linker),
    ):
        _run(tmp_path, monkeypatch, fork=False)
    assert normal_summarizer.await_count == 1 and normal_linker.await_count == 1


def test_a_fork_run_runs_no_git_log_branch_lookup_or_gh_issue_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fork_cmds: list[Sequence[str]] = []
    _run(tmp_path, monkeypatch, fork=True, commands=fork_cmds)
    assert fork_cmds == []
    normal_cmds: list[Sequence[str]] = []
    _run(tmp_path, monkeypatch, fork=False, commands=normal_cmds)
    assert any(c[0] == "gh" for c in normal_cmds) and any("log" in c for c in normal_cmds)


# --- the gates and the cost estimate use the same rule ---


@pytest.mark.parametrize("fork_run", [True, False])
def test_the_preflight_gates_follow_the_fork_flag(fork_run: bool) -> None:
    kwargs: dict[str, Any] = {
        "is_incremental": False, "agents": frozenset(), "exclude_agents": frozenset(), "fork_run": fork_run}
    assert should_run_summarizer(**kwargs) is (not fork_run)
    assert should_run_issue_linker(review_mode="full", vcs_provider="github", **kwargs) is (not fork_run)


def test_the_gates_default_to_a_normal_run() -> None:
    kwargs: dict[str, Any] = {"is_incremental": False, "agents": frozenset(), "exclude_agents": frozenset()}
    assert should_run_summarizer(**kwargs) is True
    assert should_run_issue_linker(review_mode="full", vcs_provider="github", **kwargs) is True


def test_the_cost_estimate_leaves_out_the_two_preflight_agents_on_a_fork_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[bool, list[str]] = {False: [], True: []}
    from ai_pr_review.review import cost_ceiling

    real = cost_ceiling.estimate_preflight_agent_cost
    for fork in (False, True):

        def spy(*, agent_name: str, _names: list[str] = seen[fork], **kw: Any) -> Any:
            _names.append(agent_name)
            return real(agent_name=agent_name, **kw)

        with patch("ai_pr_review.review.cost_ceiling.estimate_preflight_agent_cost", new=spy):
            _run(tmp_path, monkeypatch, fork=fork)
    assert set(seen[False]) == {"pr-summarizer", "issue-linker"}
    assert seen[True] == []


# --- the summary comment of a fork run ---


def test_a_fork_run_posts_a_fixed_non_empty_summary_on_the_first_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provider = _run(tmp_path, monkeypatch, fork=True)
    assert provider.summaries and FORK_SUMMARY_NOTICE in provider.summaries[0]
    assert not any(v in provider.summaries[0] for v in CHANNELS.values())


def test_a_fork_incremental_run_posts_no_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, provider = _run(tmp_path, monkeypatch, fork=True, incremental=True)
    assert provider.summaries == []


def test_a_normal_run_summary_has_no_fork_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, provider = _run(tmp_path, monkeypatch, fork=False)
    assert all(FORK_SUMMARY_NOTICE not in s for s in provider.summaries)
