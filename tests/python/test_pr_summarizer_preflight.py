"""Tests for ai_pr_review.review.preflight.run_summarizer's effective max_tokens (#191, #847).

run_summarizer composes its own LLMRequest (it is dispatched separately from
run_tier -- see dispatch.py's explicit refusal to dispatch pr-summarizer
generically) and, before #191, hardcoded max_tokens=4096 regardless of the
roster default or AI_MAX_TOKENS_PER_AGENT. #191 added AI_MAX_TOKENS_PR_SUMMARIZER
as an override on top of that hardcoded 4096 default without changing the
default itself. #847 fixed the default itself: the hardcoded 4096 had quietly
diverged from the roster's own max_output_tokens (16384) for pr-summarizer --
these tests confirm the no-override case now resolves 16384 from the roster,
and that AI_MAX_TOKENS_PR_SUMMARIZER still overrides it correctly.
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


def test_default_max_tokens_resolves_from_roster(
    prompt_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No AI_MAX_TOKENS_PR_SUMMARIZER set -- resolves the roster's own
    max_output_tokens (16384) for pr-summarizer, not a hand-typed literal
    (issue #847: it previously hardcoded 4096, silently diverging from the
    roster's 16384 the whole time)."""
    from ai_pr_review.agents.roster import get_agent

    monkeypatch.delenv("AI_MAX_TOKENS_PR_SUMMARIZER", raising=False)
    captured = _run(prompt_dir)
    assert len(captured) == 1
    assert captured[0].max_tokens == get_agent("pr-summarizer").max_output_tokens
    assert captured[0].max_tokens == 16384


def test_per_agent_max_tokens_override_applied(
    prompt_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI_MAX_TOKENS_PR_SUMMARIZER overrides the roster default."""
    monkeypatch.setenv("AI_MAX_TOKENS_PR_SUMMARIZER", "2048")
    captured = _run(prompt_dir)
    assert len(captured) == 1
    assert captured[0].max_tokens == 2048
