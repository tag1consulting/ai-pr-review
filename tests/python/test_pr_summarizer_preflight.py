"""Tests for ai_pr_review.review.preflight.run_summarizer's effective max_tokens (#191, #847).

run_summarizer composes its own LLMRequest (it is dispatched separately from
run_tier -- see dispatch.py's explicit refusal to dispatch pr-summarizer
generically) and, before #191, hardcoded max_tokens=4096 regardless of the
roster default or AI_MAX_TOKENS_PER_AGENT. #191 added an AI_MAX_TOKENS_PR_SUMMARIZER
override on top of that hardcoded 4096 without changing it. #847 fixed the
hardcoded 4096 itself: it had silently drifted from the roster's real
pr-summarizer default (16384, see ai_pr_review/agents/roster.py) -- the base
default with no override set is now looked up from the roster instead of a
second, independently-maintained literal. These tests confirm the roster
default is used when no override is set, and that AI_MAX_TOKENS_PR_SUMMARIZER
still overrides it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest

from ai_pr_review.llm.base import LLMRequest, LLMResponse
from ai_pr_review.review.preflight import run_summarizer

_SUMMARIZER_OUTPUT = (
    "## Summary\n\nDoes a thing.\n\n"
    "## Walkthrough\n\n| File | Change |\n|---|---|\n| a.py | edited |\n\n"
    "**Type:** feature\n**Effort:** 1/5\n"
)


def _make_response(text: str = _SUMMARIZER_OUTPUT) -> LLMResponse:
    return LLMResponse(text=text, input_tokens=10, output_tokens=20)


def _completed(stdout: str = "", returncode: int = 0) -> object:
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = ""
    return proc


@pytest.fixture()
def prompt_dir(tmp_path: Path) -> Path:
    prompt = tmp_path / "prompts" / "pr-summarizer.md"
    prompt.parent.mkdir()
    prompt.write_text("You are a test summarizer prompt.\n")
    return tmp_path


def _run(prompt_dir: Path) -> list[LLMRequest]:
    captured: list[LLMRequest] = []

    async def _fake_llm(req: LLMRequest) -> LLMResponse:
        captured.append(req)
        return _make_response()

    with patch("subprocess.run", return_value=_completed(stdout="abc1234 fix: test\n")):
        anyio.run(
            lambda: run_summarizer(
                diff_text="diff --git a/a.py b/a.py",
                manifest_text="## Manifest\n- a.py",
                base_ref="main",
                script_dir=prompt_dir,
                model="claude-haiku-4-5",
                llm_call=_fake_llm,
            )
        )
    return captured


def test_default_max_tokens_matches_roster_default(
    prompt_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No AI_MAX_TOKENS_PR_SUMMARIZER set -- the base default must come from
    the roster lookup, not a hand-typed literal that can drift from it (#847),
    even though the roster's own value is 4096 here (#850 review: the roster
    briefly held a dead, never-exercised 16384 that this fix would otherwise
    have made live for the first time with no behavior verification -- see
    roster.py's pr-summarizer comment)."""
    from ai_pr_review.agents.roster import PR_SUMMARIZER_AGENT_NAME, get_agent

    monkeypatch.delenv("AI_MAX_TOKENS_PR_SUMMARIZER", raising=False)
    captured = _run(prompt_dir)
    assert len(captured) == 1
    assert captured[0].max_tokens == get_agent(PR_SUMMARIZER_AGENT_NAME).max_output_tokens
    assert captured[0].max_tokens == 4096


def test_per_agent_max_tokens_override_applied(
    prompt_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI_MAX_TOKENS_PR_SUMMARIZER overrides the roster default."""
    monkeypatch.setenv("AI_MAX_TOKENS_PR_SUMMARIZER", "2048")
    captured = _run(prompt_dir)
    assert len(captured) == 1
    assert captured[0].max_tokens == 2048
