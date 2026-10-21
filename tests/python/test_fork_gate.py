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
    ForkGateFacts,
    decide,
    is_fork,
)
from ai_pr_review.review.runtime import SkipPlan, _apply_fork_gate, build_review_runtime

from .test_runtime import _make_config
from .vcs.test_github_summary import _make_provider

SHA_A = "a" * 40
SHA_B = "b" * 40
FORK = ForkGateFacts(head_repo="mallory/r", base_repo="o/r", approval_state="")


def _facts(**kw: str) -> ForkGateFacts:
    return ForkGateFacts(**{"head_repo": "mallory/r", "base_repo": "o/r", "approval_state": "", **kw})


# --- the decision table -----------------------------------------------------


@pytest.mark.parametrize(("head", "base", "expected"), [
    ("o/r", "o/r", False), ("O/R", "o/r", False), ("mallory/r", "o/r", True), ("", "o/r", True),
])
def test_is_fork(head: str, base: str, expected: bool) -> None:
    assert is_fork(ForkGateFacts(head_repo=head, base_repo=base, approval_state="")) is expected


def test_a_plain_pull_request_event_is_never_gated_and_needs_no_facts() -> None:
    assert decide("pull_request", SHA_A, None) == decide("pull_request", SHA_A, FORK)
    assert decide("pull_request", SHA_A, None).allowed and not decide("pull_request", SHA_A, None).gated


@pytest.mark.parametrize("event", ["pull_request_target", "issue_comment", "workflow_run", ""])
def test_a_fork_with_no_approval_is_blocked_on_every_other_event_including_an_unknown_one(event: str) -> None:
    decision = decide(event, SHA_A, FORK)
    assert decision.gated and not decision.allowed
    assert "mallory/r" in decision.reason and "aaaaaaa" in decision.reason and "ai-review-approve-fork" in decision.reason


def test_an_approved_fork_is_allowed_but_stays_gated_for_the_cost_default() -> None:
    decision = decide("pull_request_target", SHA_A, _facts(approval_state="success"))
    assert decision.gated and decision.allowed


@pytest.mark.parametrize("state", ["pending", "failure", "error"])
def test_an_approval_that_is_not_successful_does_not_count(state: str) -> None:
    decision = decide("pull_request_target", SHA_A, _facts(approval_state=state))
    assert not decision.allowed and "not in a successful state" in decision.reason


def test_a_same_repository_pull_request_is_not_gated() -> None:
    decision = decide("pull_request_target", SHA_A, _facts(head_repo="o/r"))
    assert decision.allowed and not decision.gated


def test_a_deleted_fork_is_gated_and_named_without_a_repo_name() -> None:
    decision = decide("pull_request_target", SHA_A, _facts(head_repo=""))
    assert not decision.allowed and "gone" in decision.reason


def test_unreadable_facts_fail_closed() -> None:
    decision = decide("pull_request_target", SHA_A, None)
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
        return httpx.Response(200, json=[
            {"context": "ci/other", "state": "success"},
            {"context": APPROVAL_CONTEXT, "state": "failure"},  # newest for our context
            {"context": APPROVAL_CONTEXT, "state": "success"},  # older, superseded
        ])

    facts = _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT)
    assert facts == ForkGateFacts(head_repo="mallory/r", base_repo="o/r", approval_state="failure")
    # The statuses come from the base repository. A status in the fork is never asked for.
    assert paths[1].startswith(f"/repos/o/r/commits/{SHA_A}/statuses")
    assert all("mallory" not in p for p in paths)


def test_the_facts_for_a_deleted_fork_and_no_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pr(None) if "pulls" in req.url.path else [])

    assert _github(handler).get_fork_gate_facts(SHA_A, APPROVAL_CONTEXT) == ForkGateFacts("", "o/r", "")


@pytest.mark.parametrize("bad", ["", "abc1234", "A" * 40, "g" * 40, SHA_A + "0", "../x" * 10])
def test_a_head_sha_that_is_not_a_full_commit_id_gives_no_facts_and_no_request(bad: str) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json=[])

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
        return httpx.Response(200, json=_pr("mallory/r") if is_pr else [])

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
        self.head_sha = SHA_A
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
        sha = req.url.path.split("/")[-2]
        return httpx.Response(200, json=self.statuses.get(sha, []))


async def _run(gh: _GitHub, sha: str, event: str = "pull_request_target") -> tuple[Any, list[str]]:
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
    gh.head_sha = SHA_B
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
