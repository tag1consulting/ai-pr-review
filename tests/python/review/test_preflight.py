"""Tests for ai_pr_review.review.preflight.run_summarizer.

Covers the fail-soft contract on the LLM-call site (must survive a SystemExit
from llm/client.py's call_llm(), not just a plain Exception) and the
max_tokens wiring to AgentSpec.max_output_tokens. run_issue_linker's
equivalent coverage lives in tests/python/test_issue_linker.py; judge.py's in
tests/python/test_judge.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest

from ai_pr_review.agents.roster import get_agent
from ai_pr_review.llm.base import LLMRequest, LLMResponse
from ai_pr_review.review.preflight import _SUMMARIZER_FAILURE_NOTICE
from ai_pr_review.review.preflight import run_summarizer as _run_summarizer

_VALID_SUMMARY = """\
## Summary

Adds a new widget handler.

**Type:** feature
**Effort:** 2/5 — small, self-contained change

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| src/handler.py | Added | New widget handler |
"""


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    proc: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = stderr
    return proc


@pytest.fixture()
def prompt_dir(tmp_path: Path) -> Path:
    prompt = tmp_path / "prompts" / "pr-summarizer.md"
    prompt.parent.mkdir()
    prompt.write_text("You are a test summarizer prompt.\n")
    return tmp_path


def _run(prompt_dir: Path, llm_call: object) -> str:
    with patch("subprocess.run", return_value=_completed(stdout="abc1234 fix: test commit\n")):
        return anyio.run(
            lambda: _run_summarizer(
                diff_text="diff --git a/x b/x",
                manifest_text="## Manifest\n- src/handler.py",
                base_ref="main",
                script_dir=prompt_dir,
                model="claude-haiku-4-5",
                llm_call=llm_call,
            )
        )


def test_run_summarizer_returns_llm_text_on_success(prompt_dir: Path) -> None:
    async def _fake_llm(req: LLMRequest) -> LLMResponse:
        return LLMResponse(text=_VALID_SUMMARY, input_tokens=10, output_tokens=20)

    result = _run(prompt_dir, _fake_llm)
    assert "widget handler" in result


def test_run_summarizer_never_raises_on_system_exit(prompt_dir: Path) -> None:
    """llm/client.py's call_llm() raises SystemExit (not a plain Exception) on an
    unrecoverable LLMError, e.g. thinking-budget exhaustion. SystemExit inherits
    from BaseException, not Exception, so this call site must catch BaseException
    to actually honor its documented fail-soft contract (see PR #666's CI crash)."""

    async def _exiting_llm(req: LLMRequest) -> LLMResponse:
        raise SystemExit(1)

    result = _run(prompt_dir, _exiting_llm)
    assert result == _SUMMARIZER_FAILURE_NOTICE  # fail-soft: notice, not a crash


def test_run_summarizer_never_raises_on_plain_exception(prompt_dir: Path) -> None:
    async def _exploding_llm(req: LLMRequest) -> LLMResponse:
        raise RuntimeError("simulated LLM failure")

    result = _run(prompt_dir, _exploding_llm)
    assert result == _SUMMARIZER_FAILURE_NOTICE


def test_max_tokens_matches_roster_spec(prompt_dir: Path) -> None:
    """max_tokens must be sourced from AgentSpec.max_output_tokens, not a
    hardcoded literal that can silently drift from the roster (see PR #666's
    pr-summarizer 4096-vs-16384 drift, which reporting.py's cost table had
    been silently misreporting)."""
    captured: list[LLMRequest] = []

    async def _capture_llm(req: LLMRequest) -> LLMResponse:
        captured.append(req)
        return LLMResponse(text=_VALID_SUMMARY, input_tokens=10, output_tokens=20)

    _run(prompt_dir, _capture_llm)
    assert len(captured) == 1
    assert captured[0].max_tokens == get_agent("pr-summarizer").max_output_tokens
