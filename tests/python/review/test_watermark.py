"""Tests for ai_pr_review.review.watermark.decide_watermark_advance."""

from __future__ import annotations

import pytest

from ai_pr_review.review.watermark import decide_watermark_advance

_HEAD = "abc123def4567890abc123def4567890abc123de"  # 40-char SHA


# ---------------------------------------------------------------------------
# All-success path
# ---------------------------------------------------------------------------

def test_all_succeed_advances_global() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["code-reviewer", "security-reviewer"],
        failed_agents=[],
    )
    assert policy.advance_global is True
    assert policy.new_global_sha == _HEAD
    assert policy.per_agent == {"code-reviewer": _HEAD, "security-reviewer": _HEAD}
    assert policy.body_explanation == ""


def test_no_agents_no_failures_advances_global() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=[],
        failed_agents=[],
    )
    assert policy.advance_global is True
    assert policy.new_global_sha == _HEAD
    assert policy.per_agent == {}


# ---------------------------------------------------------------------------
# Failure blocking
# ---------------------------------------------------------------------------

def test_default_any_failure_blocks_global() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["code-reviewer"],
        failed_agents=["security-reviewer"],
    )
    assert policy.advance_global is False
    assert policy.new_global_sha is None
    assert policy.per_agent == {"code-reviewer": _HEAD}
    assert "security-reviewer" in policy.body_explanation


def test_body_explanation_names_failed_agents() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=[],
        failed_agents=["security-reviewer", "edge-case-hunter"],
    )
    assert "security-reviewer" in policy.body_explanation
    assert "edge-case-hunter" in policy.body_explanation
    assert "Watermark held" in policy.body_explanation


# ---------------------------------------------------------------------------
# required_for_global — precise policy
# ---------------------------------------------------------------------------

def test_required_list_limits_block_scope() -> None:
    # Only security-reviewer is required. comment-analyzer failure doesn't block.
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["code-reviewer", "security-reviewer"],
        failed_agents=["comment-analyzer"],
        required_for_global=["code-reviewer", "security-reviewer"],
    )
    assert policy.advance_global is True
    assert policy.new_global_sha == _HEAD
    # per_agent omits the failed agent
    assert "comment-analyzer" not in policy.per_agent
    assert "code-reviewer" in policy.per_agent


def test_required_list_blocks_when_required_fails() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["code-reviewer"],
        failed_agents=["security-reviewer"],
        required_for_global=["security-reviewer"],
    )
    assert policy.advance_global is False
    assert "security-reviewer" in policy.body_explanation


def test_empty_required_never_blocks() -> None:
    # required_for_global=[] — no agent is required; global always advances
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["a"],
        failed_agents=["b", "c"],
        required_for_global=[],
    )
    assert policy.advance_global is True
    assert policy.new_global_sha == _HEAD
    assert policy.per_agent == {"a": _HEAD}


def test_default_none_means_all_ran_required() -> None:
    # Default required=None means "all agents that ran are required"
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["a"],
        failed_agents=["b"],
        required_for_global=None,
    )
    assert policy.advance_global is False


def test_required_agent_not_in_any_list_is_noop() -> None:
    # required_for_global lists an agent that wasn't dispatched at all —
    # it's neither succeeded nor failed, so it can't block.
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["code-reviewer"],
        failed_agents=[],
        required_for_global=["code-reviewer", "nonexistent-agent"],
    )
    assert policy.advance_global is True


# ---------------------------------------------------------------------------
# Conservative ambiguity handling
# ---------------------------------------------------------------------------

def test_agent_in_both_lists_treated_as_failed() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD,
        succeeded_agents=["flaky-agent"],
        failed_agents=["flaky-agent"],
    )
    assert policy.advance_global is False
    assert "flaky-agent" not in policy.per_agent
    assert "flaky-agent" in policy.body_explanation


# ---------------------------------------------------------------------------
# SHA validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_sha", ["", "   ", "not-a-sha", "XYZ", "1234567g"])
def test_invalid_sha_blocks_advance(bad_sha: str) -> None:
    policy = decide_watermark_advance(
        head_sha=bad_sha,
        succeeded_agents=["a"],
        failed_agents=[],
    )
    assert policy.advance_global is False
    assert policy.new_global_sha is None


def test_short_sha_accepted() -> None:
    policy = decide_watermark_advance(
        head_sha="abc1234",  # 7 chars, valid hex
        succeeded_agents=["a"],
        failed_agents=[],
    )
    assert policy.advance_global is True
    assert policy.new_global_sha == "abc1234"


def test_long_sha_rejected() -> None:
    # 41 chars — not a valid git SHA format
    policy = decide_watermark_advance(
        head_sha="a" * 41,
        succeeded_agents=[],
        failed_agents=[],
    )
    assert policy.advance_global is False


def test_sha_with_trailing_newline_rejected() -> None:
    # Python's `$` allows trailing \n by default; we use \A...\Z to reject it.
    policy = decide_watermark_advance(
        head_sha=_HEAD + "\n",
        succeeded_agents=[],
        failed_agents=[],
    )
    assert policy.advance_global is False


def test_sha_with_uppercase_hex_rejected() -> None:
    policy = decide_watermark_advance(
        head_sha="ABCDEF1234567890",
        succeeded_agents=[],
        failed_agents=[],
    )
    assert policy.advance_global is False


def test_sha_with_leading_whitespace_rejected() -> None:
    policy = decide_watermark_advance(
        head_sha=" " + _HEAD,
        succeeded_agents=[],
        failed_agents=[],
    )
    assert policy.advance_global is False


# ---------------------------------------------------------------------------
# WatermarkPolicy is frozen
# ---------------------------------------------------------------------------

def test_watermark_policy_is_frozen() -> None:
    policy = decide_watermark_advance(
        head_sha=_HEAD, succeeded_agents=[], failed_agents=[]
    )
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        policy.advance_global = False  # type: ignore[misc]


def test_watermark_policy_rejects_advance_with_none_sha() -> None:
    from ai_pr_review.review.watermark import WatermarkPolicy
    with pytest.raises(ValueError, match="advance_global=True requires non-None"):
        WatermarkPolicy(
            advance_global=True,
            new_global_sha=None,
            per_agent={},
            body_explanation="",
        )


def test_watermark_policy_rejects_blocked_with_sha() -> None:
    from ai_pr_review.review.watermark import WatermarkPolicy
    with pytest.raises(ValueError, match="advance_global=False requires new_global_sha=None"):
        WatermarkPolicy(
            advance_global=False,
            new_global_sha=_HEAD,
            per_agent={},
            body_explanation="",
        )
