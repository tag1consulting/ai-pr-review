"""The fork gate (review/fork_gate.py): the decision table, the GitHub facts, and the hostile sequence
label, push, run through the real provider and ``build_review_runtime``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from ai_pr_review.config import ReviewConfig
from ai_pr_review.review.fork_gate import (
    APPROVAL_CONTEXT,
    DEFAULT_FORK_MAX_COST_USD,
    LLM_KEY_VARS,
    ForkGateFacts,
    checkout_mismatch_reason,
    decide,
    is_fork,
    llm_key_present,
    needs_facts,
)
from ai_pr_review.review.fork_gate import (
    workspace_head as _real_workspace_head,  # before the fixture patches it
)
from ai_pr_review.review.runtime import SkipPlan, _apply_fork_gate, build_review_runtime

from .test_runtime import _make_config
from .vcs.test_github_summary import _make_provider

SHA_A = "a" * 40
SHA_B = "b" * 40
FORK = ForkGateFacts(head_repo="mallory/r", base_repo="o/r", approval_state="")
_CHECKED_OUT: dict[str, str | None] = {"sha": None}  # None: the workspace is at the asked commit


@pytest.fixture(autouse=True)
def _workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    _CHECKED_OUT["sha"] = None
    for var in LLM_KEY_VARS:  # a key in the test environment must not decide what a test means
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "ai_pr_review.review.fork_gate.workspace_head", lambda workspace=".": _CHECKED_OUT["sha"] or SHA_A)


def _facts(**kw: str) -> ForkGateFacts:
    return ForkGateFacts(**{"head_repo": "mallory/r", "base_repo": "o/r", "approval_state": "", **kw})


# --- the decision table -----------------------------------------------------


@pytest.mark.parametrize(("head", "base", "expected"), [
    ("o/r", "o/r", False), ("O/R", "o/r", False), ("mallory/r", "o/r", True), ("", "o/r", True),
])
def test_is_fork(head: str, base: str, expected: bool) -> None:
    assert is_fork(ForkGateFacts(head_repo=head, base_repo=base, approval_state="")) is expected


def test_a_plain_pull_request_event_without_a_key_is_not_gated_and_needs_no_facts() -> None:
    for facts in (None, FORK):
        decision = decide("pull_request", SHA_A, facts, key_present=False)
        assert decision.allowed and not decision.gated
    assert not needs_facts("pull_request", key_present=False)


def test_a_plain_pull_request_event_with_a_key_is_gated_like_any_other() -> None:
    # A private repository can send secrets to fork pull requests. A key in the environment shows that it did.
    assert needs_facts("pull_request", key_present=True)
    assert not decide("pull_request", SHA_A, FORK, key_present=True).allowed
    assert decide("pull_request", SHA_A, None, key_present=True).gated


@pytest.mark.parametrize(("name", "value", "present"), [
    ("ANTHROPIC_API_KEY", "x", True), ("OPENAI_API_KEY", " ", False), ("GOOGLE_API_KEY", "", False),
    ("BEDROCK_API_KEY", "x", True),
])
def test_llm_key_present_sees_each_provider_key(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, present: bool,
) -> None:
    for var in LLM_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert not llm_key_present()
    monkeypatch.setenv(name, value)
    assert llm_key_present() is present


@pytest.mark.parametrize("event", ["pull_request_target", "issue_comment", "workflow_run", ""])
def test_a_fork_with_no_approval_is_blocked_on_every_other_event_including_an_unknown_one(event: str) -> None:
    decision = decide(event, SHA_A, FORK, key_present=True)
    assert decision.gated and not decision.allowed
    assert "mallory/r" in decision.reason and "aaaaaaa" in decision.reason and "ai-review-approve-fork" in decision.reason
    assert "full pull request diff" in decision.reason  # not just the top commit


def test_an_approved_fork_is_allowed_but_stays_gated_for_the_cost_default() -> None:
    decision = decide("pull_request_target", SHA_A, _facts(approval_state="success"), key_present=True)
    assert decision.gated and decision.allowed


@pytest.mark.parametrize("state", ["pending", "failure", "error"])
def test_an_approval_that_is_not_successful_does_not_count(state: str) -> None:
    decision = decide("pull_request_target", SHA_A, _facts(approval_state=state), key_present=True)
    assert not decision.allowed and "not in a successful state" in decision.reason


def test_a_same_repository_pull_request_is_not_gated() -> None:
    decision = decide("pull_request_target", SHA_A, _facts(head_repo="o/r"), key_present=True)
    assert decision.allowed and not decision.gated


def test_a_deleted_fork_is_gated_and_named_without_a_repo_name() -> None:
    decision = decide("pull_request_target", SHA_A, _facts(head_repo=""), key_present=True)
    assert not decision.allowed and "gone" in decision.reason


def test_unreadable_facts_fail_closed() -> None:
    decision = decide("pull_request_target", SHA_A, None, key_present=True)
    assert decision.gated and not decision.allowed and "could not be checked" in decision.reason


# --- the GitHub facts -------------------------------------------------------


def _github(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    return _make_provider(handler)[0]


def _pr(head_repo: str | None, base_repo: str = "o/r") -> dict[str, Any]:
    return {"base": {"repo": {"full_name": base_repo}},
            "head": {"repo": None if head_repo is None else {"full_name": head_repo}}}


def test_the_facts_read_the_base_repository_statuses_and_take_the_newest_for_the_context() -> None:
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.raw_path.decode())
        if req.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json=_pr("mallory/r"))
        return httpx.Response(200, json={"state": "failure", "statuses": [
            {"context": "ci/other", "state": "success"},
            {"context": APPROVAL_CONTEXT, "state": "failure"},  # the combined endpoint gives one per context
        ]})

    facts = _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT)
    assert facts == ForkGateFacts(head_repo="mallory/r", base_repo="o/r", approval_state="failure")
    # The statuses come from the base repository. A status in the fork is never asked for.
    assert paths[1] == f"/repos/o/r/commits/{SHA_A}/status"
    assert all("mallory" not in p for p in paths)


def test_the_facts_for_a_deleted_fork_and_no_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pr(None) if "pulls" in req.url.path else {"statuses": []})

    assert _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT) == ForkGateFacts("", "o/r", "")


@pytest.mark.parametrize("bad", ["", "abc1234", "A" * 40, "g" * 40, SHA_A + "0", "../x" * 10])
def test_a_head_sha_that_is_not_a_full_commit_id_gives_no_facts_and_no_request(bad: str) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"statuses": []})

    provider = _github(handler)
    assert provider.get_fork_gate_facts(bad, APPROVAL_CONTEXT) is None
    assert calls == [] and provider._errors


@pytest.mark.parametrize("which", ["pr", "statuses"])
@pytest.mark.parametrize("response", [
    httpx.Response(404, text="nope"), httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"unexpected": "shape"}), httpx.Response(200, json="text"),
])
def test_any_bad_response_gives_none(which: str, response: httpx.Response) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        is_pr = req.url.path.endswith("/pulls/7")
        if (which == "pr") == is_pr:
            return response
        return httpx.Response(200, json=_pr("mallory/r") if is_pr else {"statuses": []})

    assert _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT) is None


# --- config and the cost default -------------------------------------------


def test_the_event_name_and_the_fork_ceiling_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", " pull_request_target ")
    monkeypatch.setenv("AI_FORK_MAX_COST_USD", "3.5")
    cfg = ReviewConfig.from_env()
    assert cfg.event_name == "pull_request_target" and cfg.fork_max_cost_usd == 3.5
    monkeypatch.delenv("AI_FORK_MAX_COST_USD")
    assert ReviewConfig.from_env().fork_max_cost_usd == DEFAULT_FORK_MAX_COST_USD


@pytest.mark.parametrize("bad", ["-1", "nan", "inf"])
def test_an_unusable_fork_ceiling_falls_back_to_the_default_and_never_switches_it_off(
    monkeypatch: pytest.MonkeyPatch, bad: str,
) -> None:
    monkeypatch.setenv("AI_FORK_MAX_COST_USD", bad)
    assert ReviewConfig.from_env().fork_max_cost_usd == DEFAULT_FORK_MAX_COST_USD


class _Facts:
    def __init__(self, facts: ForkGateFacts | None) -> None:
        self.facts, self.asked = facts, []

    def get_fork_gate_facts(self, head_sha: str, context: str) -> ForkGateFacts | None:
        self.asked.append((head_sha, context))
        return self.facts


def _gate(event: str, facts: ForkGateFacts | None, **cfg: object) -> tuple[ReviewConfig, SkipPlan | None]:
    return _apply_fork_gate(_make_config(event_name=event, head_sha=SHA_A, **cfg), _Facts(facts))  # type: ignore[arg-type]


def test_an_approved_fork_gets_the_default_cost_ceiling_and_an_explicit_ceiling_wins() -> None:
    config, skip = _gate("pull_request_target", _facts(approval_state="success"))
    assert skip is None and config.max_cost_usd == DEFAULT_FORK_MAX_COST_USD
    config, _ = _gate("pull_request_target", _facts(approval_state="success"), max_cost_usd=9.0)
    assert config.max_cost_usd == 9.0
    config, _ = _gate("pull_request_target", _facts(approval_state="success"), fork_max_cost_usd=0.0)
    assert config.max_cost_usd == 0.0  # 0 switches the default off
    config, _ = _gate("pull_request_target", _facts(head_repo="o/r"))
    assert config.max_cost_usd == 0.0  # a same-repository pull request keeps its own setting


def test_a_pull_request_event_makes_no_facts_request() -> None:
    provider = _Facts(FORK)
    config = _make_config(event_name="pull_request", head_sha=SHA_A)
    assert _apply_fork_gate(config, provider) == (config, None)  # type: ignore[arg-type]
    assert provider.asked == []


def test_a_provider_without_the_method_is_not_gated() -> None:
    config = _make_config(event_name="pull_request_target", head_sha=SHA_A)
    assert _apply_fork_gate(config, object()) == (config, None)  # type: ignore[arg-type]


# --- the hostile sequence: label, push, run --------------------------------


class _GitHub:
    """A stateful stand-in for the three GitHub calls the gate makes."""

    def __init__(self) -> None:
        self.head_repo: str | None = "mallory/r"
        self.statuses: dict[str, list[dict[str, str]]] = {}  # sha -> statuses, newest first
        self.fail = False
        self.paths: list[str] = []

    def approve(self, sha: str) -> None:  # what the trusted workflow does after the label
        self.statuses.setdefault(sha, []).insert(0, {"context": APPROVAL_CONTEXT, "state": "success"})

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.paths.append(req.url.path)
        if self.fail:
            return httpx.Response(503, text="down")
        if req.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json=_pr(self.head_repo))
        if req.url.path.endswith("/status"):
            sha = req.url.path.split("/")[-2]
            return httpx.Response(200, json={"statuses": self.statuses.get(sha, [])})
        return httpx.Response(200, json=[])  # comments, reviews: nothing yet


async def _run(
    gh: _GitHub, sha: str, event: str = "pull_request_target", checked_out: str | None = None,
) -> tuple[Any, list[str]]:
    _CHECKED_OUT["sha"] = checked_out or sha  # the workflow checked out the commit it ran for
    provider = _github(gh.handler)
    computed: list[str] = []

    def fake_compute(config: ReviewConfig, **_kw: object) -> dict[str, object]:
        computed.append(config.head_sha)  # git work on the checkout would start here
        return {"skip": True, "reason": "NO-CHANGES"}

    config = _make_config(event_name=event, head_sha=sha)
    with patch("ai_pr_review.review.runtime.run_compute", fake_compute):
        result = await build_review_runtime(config, provider_factory=lambda: provider)
    return result, computed


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_label_push_run_the_new_commit_is_never_reviewed(anyio_backend: str) -> None:
    gh = _GitHub()
    # 1. A fork pull request with no approval: paused, and no git work starts.
    result, computed = await _run(gh, SHA_A)
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and computed == []
    # 2. A maintainer labels. The trusted workflow records approval for the commit it saw.
    gh.approve(SHA_A)
    result, computed = await _run(gh, SHA_A)
    assert computed == [SHA_A] and not result.is_fork_gate_skip  # the review proceeds
    # 3. The author pushes new code. The new commit has no approval.
    result, computed = await _run(gh, SHA_B)
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and computed == []
    # 4. The old approval still exists, but it is bound to the old commit.
    assert gh.statuses[SHA_A] and SHA_B not in gh.statuses
    assert all("mallory" not in p for p in gh.paths)  # only the base repository is asked


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_an_api_failure_blocks_the_run_before_any_git_work(anyio_backend: str) -> None:
    gh = _GitHub()
    gh.fail = True
    result, computed = await _run(gh, SHA_A)
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and computed == []
    assert "could not be checked" in result.reason


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_same_repository_pull_request_and_a_plain_pull_request_event_run_without_approval(
    anyio_backend: str,
) -> None:
    gh = _GitHub()
    gh.head_repo = "o/r"
    _, computed = await _run(gh, SHA_A)
    assert computed == [SHA_A]
    gh.head_repo, gh.paths = "mallory/r", []
    _, computed = await _run(gh, SHA_B, event="pull_request")
    assert computed == [SHA_B] and "/repos/o/r/pulls/7" not in gh.paths  # no facts request at all


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_failed_approval_after_a_success_blocks_again(anyio_backend: str) -> None:
    gh = _GitHub()
    gh.approve(SHA_A)
    gh.statuses[SHA_A].insert(0, {"context": APPROVAL_CONTEXT, "state": "failure"})  # a revocation
    result, computed = await _run(gh, SHA_A)
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and computed == []


def test_this_repository_uses_the_same_approval_workflow_as_the_example() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    own = (root / ".github" / "workflows" / "fork-approval.yml").read_text()
    assert own == (root / "examples" / "workflows" / "fork-approval.yml").read_text()
    # The approval job must stay free of a checkout and of any expression inside a run script.
    assert "actions/checkout" not in own
    import re

    for script in re.findall(r"run: \|\n((?:\s{10}.*\n|\n)+)", own):
        assert "${{" not in script


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_an_approved_commit_does_not_cover_a_workspace_at_another_commit(anyio_backend: str) -> None:
    gh = _GitHub()
    gh.approve(SHA_A)
    # The workflow checked out a branch name, and the author pushed commit B after the approval.
    result, computed = await _run(gh, SHA_A, checked_out=SHA_B)
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and computed == []
    assert "bbbbbbb" in result.reason and "aaaaaaa" in result.reason and "head.sha" in result.reason


def test_the_mismatch_text_is_empty_only_for_the_same_commit() -> None:
    assert checkout_mismatch_reason(SHA_A, SHA_A) == ""
    assert "unknown commit" in checkout_mismatch_reason(SHA_A, None)
    assert checkout_mismatch_reason(SHA_A, SHA_B)


def test_workspace_head_reads_the_checked_out_commit_and_fails_to_none(tmp_path: Any) -> None:
    import subprocess

    real = _real_workspace_head
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e.x"}
    for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(repo), *cmd], env=env, check=True)
    expected = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    assert real(str(repo)) == expected
    assert real(str(tmp_path / "missing")) is None
    assert real(str(tmp_path)) is None  # not a repository


# --- review round: permissions, logging, moved pull request, exit code ---------


def test_a_same_repository_pull_request_never_asks_for_statuses() -> None:
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json=_pr("o/r"))
        return httpx.Response(403, text="Resource not accessible by integration")  # no statuses: read

    facts = _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT)
    assert facts == ForkGateFacts("o/r", "o/r", "") and paths == ["/repos/o/r/pulls/7"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_same_repository_pull_request_still_runs_when_statuses_are_forbidden(anyio_backend: str) -> None:
    gh = _GitHub()
    gh.head_repo = "o/r"
    original = gh.handler

    def forbidding(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="no") if req.url.path.endswith("/status") else original(req)

    gh.handler = forbidding  # type: ignore[method-assign]
    _, computed = await _run(gh, SHA_A)
    assert computed == [SHA_A]


def test_a_forbidden_statuses_read_on_a_fork_is_logged_with_the_permission_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json=_pr("mallory/r"))
        return httpx.Response(403, text="Resource not accessible by integration")

    with caplog.at_level("WARNING"):
        assert _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT) is None
    assert "statuses HTTP 403" in caplog.text and "statuses: read" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_pull_request_that_moved_on_still_reviews_the_approved_commit_it_checked_out(
    anyio_backend: str,
) -> None:
    # Approved A, the author pushed B, an old job for A is re-run with the workspace at A: the approved
    # code is what is reviewed, so it runs. The workspace check is what blocks a mismatched tree.
    gh = _GitHub()
    gh.approve(SHA_A)
    result, computed = await _run(gh, SHA_A, checked_out=SHA_A)
    assert computed == [SHA_A] and not result.is_fork_gate_skip


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_pull_request_event_with_a_key_present_is_gated_for_a_fork(
    anyio_backend: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = _GitHub()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    result, computed = await _run(gh, SHA_A, event="pull_request")
    assert isinstance(result, SkipPlan) and result.is_fork_gate_skip and computed == []
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    for var in LLM_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    gh.paths = []
    _, computed = await _run(gh, SHA_A, event="pull_request")
    assert computed == [SHA_A] and gh.paths == [p for p in gh.paths if "/pulls/7" not in p]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize(("opt_in", "code", "outcome"), [(False, 0, "skipped"), (True, 2, "skipped_fork_gate_failed")])
async def test_a_fork_gate_pause_exits_zero_unless_the_operator_opts_in(
    tmp_path: Any, anyio_backend: str, opt_in: bool, code: int, outcome: str,
) -> None:
    import json
    from unittest.mock import AsyncMock, MagicMock

    from ai_pr_review.cli import _run_review_async
    from ai_pr_review.vcs.protocol import SummaryResult, VcsProvider

    from .test_cli import _make_config as cli_config

    sink = tmp_path / "telemetry.jsonl"
    config = cli_config(fail_on_fork_gate=opt_in, telemetry_enabled=True, telemetry_sink=f"file://{sink}")
    provider = MagicMock(spec=VcsProvider)
    provider.post_skip_comment.return_value = SummaryResult(comment_id=1, created=True, updated=False)
    plan = SkipPlan(reason="This pull request comes from a fork", provider=provider, is_fork_gate_skip=True)
    with patch("ai_pr_review.review.runtime.build_review_runtime", new=AsyncMock(return_value=plan)):
        assert await _run_review_async(config) == code
    assert provider.post_skip_comment.call_args.args[0] == "This pull request comes from a fork"
    assert json.loads(sink.read_text().splitlines()[0])["outcome"] == outcome


def test_the_fail_on_fork_gate_setting_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not ReviewConfig.from_env().fail_on_fork_gate
    monkeypatch.setenv("AI_FAIL_ON_FORK_GATE", "true")
    assert ReviewConfig.from_env().fail_on_fork_gate
