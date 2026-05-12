"""Tests for ai_pr_review.agents.dispatch."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from ai_pr_review.agents.dispatch import (
    AgentResult,
    DispatchContext,
    FailedAgent,
    TokenUsage,
    cache_priming_effective,
    effective_prompt,
    run_tier,
)
from ai_pr_review.agents.roster import AGENTS, get_agent
from ai_pr_review.llm.base import LLMResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(text: str = "NONE", stop_reason: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        text=text,
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        stop_reason=stop_reason,
    )


def _make_context(tmp_path: Path) -> DispatchContext:
    diff = tmp_path / "diff.txt"
    diff.write_text("diff content")
    # Create minimal prompts directory so effective_prompt() can resolve files.
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "_knowledge-cutoff.md").write_text("## cutoff\n")
    (prompts / "_trailer-findings.md").write_text("## trailer\n")
    (prompts / "suggestion-addendum.md").write_text("## suggestions\n")
    for agent_name in (
        "code-reviewer", "silent-failure-hunter", "architecture-reviewer",
        "security-reviewer", "blind-hunter", "edge-case-hunter",
        "adversarial-general", "pr-summarizer",
    ):
        (prompts / f"{agent_name}.md").write_text(f"## {agent_name} prompt\n")
    return DispatchContext(
        script_dir=tmp_path,
        mode="full",
        diff_path=diff,
        provider="anthropic",
        standard_model="claude-test",
    )


# ---------------------------------------------------------------------------
# T1: AgentResult, FailedAgent, TokenUsage
# ---------------------------------------------------------------------------

def test_agent_result_fields() -> None:
    usage = TokenUsage(input=100, output=50, cache_creation=0, cache_read=0, model="claude-test")
    result = AgentResult(name="code-reviewer", output="NONE", token_log=usage, truncated=False)
    assert result.name == "code-reviewer"
    assert result.output == "NONE"
    assert result.token_log is usage
    assert result.truncated is False


def test_agent_result_truncated_from_stop_reason() -> None:
    resp = _make_response(stop_reason="max_tokens")
    usage = TokenUsage(
        input=resp.input_tokens,
        output=resp.output_tokens,
        cache_creation=resp.cache_creation_tokens,
        cache_read=resp.cache_read_tokens,
        model="m",
    )
    result = AgentResult(
        name="x",
        output=resp.text,
        token_log=usage,
        truncated=resp.stop_reason in ("max_tokens", "length", "MAX_TOKENS"),
    )
    assert result.truncated is True


def test_failed_agent_fields() -> None:
    fa = FailedAgent(name="blind-hunter", reason="timeout", exit_code=2, elapsed_ms=3000)
    assert fa.name == "blind-hunter"
    assert fa.exit_code == 2
    assert fa.elapsed_ms == 3000


def test_token_usage_fields() -> None:
    u = TokenUsage(input=1, output=2, cache_creation=3, cache_read=4, model="m")
    assert u.input == 1
    assert u.output == 2
    assert u.cache_creation == 3
    assert u.cache_read == 4
    assert u.model == "m"


# ---------------------------------------------------------------------------
# T2: run_tier
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_tier_happy_path(tmp_path: Path) -> None:
    """All agents succeed — results returned, no failures."""
    ctx = _make_context(tmp_path)
    agents = [get_agent("code-reviewer"), get_agent("pr-summarizer")]

    async def mock_llm(request: object) -> LLMResponse:
        return _make_response("findings from agent")

    successes, failures = await run_tier(
        agents=agents,
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=3,
    )
    assert len(successes) == 2
    assert len(failures) == 0
    names = {r.name for r in successes}
    assert names == {"code-reviewer", "pr-summarizer"}


@pytest.mark.anyio
async def test_run_tier_partial_failure(tmp_path: Path) -> None:
    """One agent raises — recorded as FailedAgent; others complete."""
    ctx = _make_context(tmp_path)
    agents = [get_agent("code-reviewer"), get_agent("silent-failure-hunter")]
    call_count = 0

    async def mock_llm(request: object) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated LLM failure")
        return _make_response("ok")

    successes, failures = await run_tier(
        agents=agents,
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=3,
    )
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].name in {"code-reviewer", "silent-failure-hunter"}
    assert "simulated LLM failure" in failures[0].reason


@pytest.mark.anyio
async def test_run_tier_all_fail(tmp_path: Path) -> None:
    """All agents fail — empty successes, all recorded as failures."""
    ctx = _make_context(tmp_path)
    agents = [get_agent("code-reviewer")]

    async def mock_llm(request: object) -> LLMResponse:
        raise RuntimeError("always fails")

    successes, failures = await run_tier(
        agents=agents,
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=3,
    )
    assert len(successes) == 0
    assert len(failures) == 1


@pytest.mark.anyio
async def test_run_tier_respects_semaphore(tmp_path: Path) -> None:
    """Concurrent count never exceeds semaphore_size."""
    ctx = _make_context(tmp_path)
    # Use all 8 agents to stress the semaphore
    agents = list(AGENTS)
    concurrent_peak = 0
    active = 0

    async def mock_llm(request: object) -> LLMResponse:
        nonlocal concurrent_peak, active
        active += 1
        concurrent_peak = max(concurrent_peak, active)
        await anyio.sleep(0)  # yield to allow other coroutines to start
        active -= 1
        return _make_response()

    await run_tier(agents=agents, llm_call=mock_llm, context=ctx, semaphore_size=3)
    assert concurrent_peak <= 3


# ---------------------------------------------------------------------------
# T3: effective_prompt
# ---------------------------------------------------------------------------

def _make_prompt_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal prompts directory under tmp_path; return (script_dir, base_prompt)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "_knowledge-cutoff.md").write_text("## knowledge cutoff\n")
    (prompts / "_trailer-findings.md").write_text("## findings trailer\n")
    (prompts / "suggestion-addendum.md").write_text("## suggestion addendum\n")
    base = prompts / "code-reviewer.md"
    base.write_text("## code reviewer base\n")
    return tmp_path, base


def test_effective_prompt_finding_agent_gets_trailers(tmp_path: Path) -> None:
    """Finding-producing agents get knowledge-cutoff + findings-trailer appended."""
    script_dir, base = _make_prompt_dir(tmp_path)
    result = effective_prompt("code-reviewer", base, script_dir, enable_suggestions=False)
    content = result.read_text()
    assert "code reviewer base" in content
    assert "knowledge cutoff" in content
    assert "findings trailer" in content
    assert "suggestion addendum" not in content


def test_effective_prompt_summarizer_passthrough(tmp_path: Path) -> None:
    """pr-summarizer passes through unchanged — no trailers."""
    script_dir, _ = _make_prompt_dir(tmp_path)
    base = script_dir / "prompts" / "pr-summarizer.md"
    base.write_text("## summarizer base\n")
    result = effective_prompt("pr-summarizer", base, script_dir, enable_suggestions=False)
    # Should return the original path unchanged
    assert result == base
    assert result.read_text() == "## summarizer base\n"


def test_effective_prompt_suggestion_addendum_when_enabled(tmp_path: Path) -> None:
    """Suggestion-eligible agents get addendum when enable_suggestions=True."""
    script_dir, base = _make_prompt_dir(tmp_path)
    result = effective_prompt("code-reviewer", base, script_dir, enable_suggestions=True)
    content = result.read_text()
    assert "suggestion addendum" in content


def test_effective_prompt_no_suggestion_for_architecture_reviewer(tmp_path: Path) -> None:
    """architecture-reviewer is NOT in the suggestion set."""
    script_dir, _ = _make_prompt_dir(tmp_path)
    base = script_dir / "prompts" / "architecture-reviewer.md"
    base.write_text("## arch base\n")
    result = effective_prompt("architecture-reviewer", base, script_dir, enable_suggestions=True)
    content = result.read_text()
    assert "findings trailer" in content
    assert "suggestion addendum" not in content


def test_effective_prompt_missing_trailer_raises(tmp_path: Path) -> None:
    """Missing findings trailer raises FileNotFoundError — it's a required file."""
    script_dir, base = _make_prompt_dir(tmp_path)
    (script_dir / "prompts" / "_trailer-findings.md").unlink()
    with pytest.raises(FileNotFoundError, match="findings trailer"):
        effective_prompt("code-reviewer", base, script_dir, enable_suggestions=False)


def test_effective_prompt_missing_base_raises(tmp_path: Path) -> None:
    """Missing base prompt raises FileNotFoundError."""
    script_dir, _ = _make_prompt_dir(tmp_path)
    missing = script_dir / "prompts" / "nonexistent.md"
    with pytest.raises(FileNotFoundError, match="base prompt not found"):
        effective_prompt("code-reviewer", missing, script_dir, enable_suggestions=False)


# ---------------------------------------------------------------------------
# T4: cache_priming_effective
# ---------------------------------------------------------------------------

def test_cache_priming_off_by_default() -> None:
    assert cache_priming_effective("anthropic", "false", "auto") is False


def test_cache_priming_on_for_anthropic() -> None:
    assert cache_priming_effective("anthropic", "true", "auto") is True


def test_cache_priming_on_for_bedrock_proxy() -> None:
    assert cache_priming_effective("bedrock-proxy", "true", "auto") is True


def test_cache_priming_off_for_openai() -> None:
    assert cache_priming_effective("openai", "true", "auto") is False


def test_cache_priming_off_for_google() -> None:
    assert cache_priming_effective("google", "true", "auto") is False


def test_cache_priming_off_when_prompt_caching_disabled() -> None:
    assert cache_priming_effective("anthropic", "true", "false") is False


def test_cache_priming_on_when_prompt_caching_explicit_true() -> None:
    assert cache_priming_effective("anthropic", "true", "true") is True


def test_cache_priming_truthy_variants() -> None:
    for truthy in ("true", "TRUE", "True", "1"):
        assert cache_priming_effective("anthropic", truthy, "auto") is True, f"failed for {truthy!r}"


def test_cache_priming_falsy_variants() -> None:
    for falsy in ("false", "FALSE", "False", "0", ""):
        assert cache_priming_effective("anthropic", falsy, "auto") is False, f"failed for {falsy!r}"
