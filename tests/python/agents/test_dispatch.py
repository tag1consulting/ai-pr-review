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


def test_dispatch_context_rejects_empty_standard_model(tmp_path: Path) -> None:
    diff = tmp_path / "diff.txt"
    diff.write_text("")
    with pytest.raises(ValueError, match="standard_model must be non-empty"):
        DispatchContext(
            script_dir=tmp_path,
            mode="full",
            diff_path=diff,
            provider="anthropic",
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
    # pr-summarizer is intentionally excluded; it composes its own prompt
    # via ai_pr_review.agents.summarizer and must not be dispatched here.
    agents = [get_agent("code-reviewer"), get_agent("silent-failure-hunter")]

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
    assert names == {"code-reviewer", "silent-failure-hunter"}


@pytest.mark.anyio
async def test_run_tier_propagates_prompt_degraded(tmp_path: Path) -> None:
    """AgentResult.prompt_degraded reflects missing suggestion-addendum."""
    ctx = _make_context(tmp_path)
    # Remove the addendum so code-reviewer (suggestion-eligible) is degraded
    (tmp_path / "prompts" / "suggestion-addendum.md").unlink()

    async def mock_llm(request: object) -> LLMResponse:
        return _make_response("ok")

    successes, failures = await run_tier(
        agents=[get_agent("code-reviewer"), get_agent("architecture-reviewer")],
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=2,
    )
    assert len(successes) == 2
    by_name = {r.name: r for r in successes}
    assert by_name["code-reviewer"].prompt_degraded is True
    # architecture-reviewer isn't in the suggestion set → not degraded
    assert by_name["architecture-reviewer"].prompt_degraded is False


@pytest.mark.anyio
async def test_run_tier_rejects_pr_summarizer(tmp_path: Path) -> None:
    """pr-summarizer must not be dispatched via run_tier — it needs
    its own prompt/message composition via the summarizer module."""
    ctx = _make_context(tmp_path)
    agents = [get_agent("pr-summarizer")]

    async def mock_llm(request: object) -> LLMResponse:
        return _make_response("should not be called")

    successes, failures = await run_tier(
        agents=agents,
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=1,
    )
    assert len(successes) == 0
    assert len(failures) == 1
    assert failures[0].name == "pr-summarizer"
    assert "summarizer" in failures[0].reason.lower()


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


def test_format_exception_chain_marks_cycles() -> None:
    from ai_pr_review.agents.dispatch import _format_exception_chain
    a = RuntimeError("outer")
    b = ValueError("inner")
    # Build a cycle: a.__cause__ is b, b.__cause__ is a
    a.__cause__ = b
    b.__cause__ = a
    rendered = _format_exception_chain(a)
    assert "RuntimeError: outer" in rendered
    assert "ValueError: inner" in rendered
    assert "<cycle detected>" in rendered


def test_format_exception_chain_marks_deep_truncation() -> None:
    from ai_pr_review.agents.dispatch import _format_exception_chain
    # Build a chain of 20 exceptions — depth cap is 16
    chain: list[BaseException] = [RuntimeError(f"level-{i}") for i in range(20)]
    for i in range(len(chain) - 1):
        chain[i].__cause__ = chain[i + 1]
    rendered = _format_exception_chain(chain[0])
    assert "<truncated after 16 links>" in rendered
    # First 16 should be present, level-16 and beyond should not
    assert "level-0" in rendered
    assert "level-15" in rendered
    assert "level-16" not in rendered


@pytest.mark.anyio
async def test_run_tier_captures_exception_chain(tmp_path: Path) -> None:
    """FailedAgent.reason preserves __cause__ chain for nested exceptions."""
    ctx = _make_context(tmp_path)
    agents = [get_agent("code-reviewer")]

    async def mock_llm(request: object) -> LLMResponse:
        try:
            raise ValueError("upstream problem")
        except ValueError as ve:
            raise RuntimeError("wrapper failed") from ve

    successes, failures = await run_tier(
        agents=agents,
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=1,
    )
    assert len(failures) == 1
    reason = failures[0].reason
    assert "RuntimeError: wrapper failed" in reason
    assert "ValueError: upstream problem" in reason
    assert "caused by" in reason


@pytest.mark.anyio
async def test_run_tier_isolates_systemexit_from_llm_call(tmp_path: Path) -> None:
    """SystemExit from llm_call (per llm/client.py contract) must be isolated.

    Regression test: dispatch.py previously caught `Exception`, so SystemExit
    (BaseException subclass) escaped the handler and propagated through the
    anyio task group, cancelling all sibling agents. The fix catches
    BaseException and synthesizes a FailedAgent with exit_code = exc.code.
    """
    ctx = _make_context(tmp_path)
    agents = [get_agent("code-reviewer"), get_agent("silent-failure-hunter")]
    call_count = 0

    async def mock_llm(request: object) -> LLMResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Mirrors llm/client.py: SystemExit(2) on retries exhausted.
            raise SystemExit(2)
        return _make_response("ok")

    successes, failures = await run_tier(
        agents=agents,
        llm_call=mock_llm,
        context=ctx,
        semaphore_size=1,  # Force ordered execution so the SystemExit fires first.
    )
    # Sibling must still complete; the SystemExit-raising one must be a FailedAgent.
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].exit_code == 2  # Preserved from SystemExit.code.


@pytest.mark.anyio
async def test_run_tier_uses_premium_model_for_tier2_full(tmp_path: Path) -> None:
    """Tier-2 agents in full mode use premium_model; tier-1 always use standard_model."""
    # Construct directly with the intended values — mutating the context
    # after construction would silently break if DispatchContext is frozen.
    base = _make_context(tmp_path)
    ctx = DispatchContext(
        script_dir=base.script_dir,
        mode="full",
        diff_path=base.diff_path,
        provider="anthropic",
        standard_model="claude-test",
        premium_model="claude-premium",
    )
    seen_models: list[str] = []

    async def mock_llm(request: object) -> LLMResponse:
        from ai_pr_review.llm.base import LLMRequest
        assert isinstance(request, LLMRequest)
        seen_models.append(request.model_id)
        return _make_response("ok")

    tier1 = [get_agent("code-reviewer")]      # tier 1
    tier2 = [get_agent("architecture-reviewer")]  # tier 2, full_mode_only

    await run_tier(agents=tier1, llm_call=mock_llm, context=ctx, semaphore_size=3)
    await run_tier(agents=tier2, llm_call=mock_llm, context=ctx, semaphore_size=3)

    assert seen_models[0] == "claude-test"    # tier 1 always standard
    assert seen_models[1] == "claude-premium"  # tier 2 full → premium


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
    path, degraded = effective_prompt(
        "code-reviewer", base, script_dir, enable_suggestions=False
    )
    content = path.read_text()
    assert "code reviewer base" in content
    assert "knowledge cutoff" in content
    assert "findings trailer" in content
    assert "suggestion addendum" not in content
    assert degraded is False


def test_effective_prompt_summarizer_passthrough(tmp_path: Path) -> None:
    """pr-summarizer passes through unchanged — no trailers, not degraded."""
    script_dir, _ = _make_prompt_dir(tmp_path)
    base = script_dir / "prompts" / "pr-summarizer.md"
    base.write_text("## summarizer base\n")
    path, degraded = effective_prompt(
        "pr-summarizer", base, script_dir, enable_suggestions=False
    )
    assert path == base
    assert path.read_text() == "## summarizer base\n"
    assert degraded is False


def test_effective_prompt_suggestion_addendum_when_enabled(tmp_path: Path) -> None:
    """Suggestion-eligible agents get addendum when enable_suggestions=True."""
    script_dir, base = _make_prompt_dir(tmp_path)
    path, degraded = effective_prompt(
        "code-reviewer", base, script_dir, enable_suggestions=True
    )
    content = path.read_text()
    assert "suggestion addendum" in content
    assert degraded is False


def test_effective_prompt_no_suggestion_for_architecture_reviewer(tmp_path: Path) -> None:
    """architecture-reviewer is NOT in the suggestion set."""
    script_dir, _ = _make_prompt_dir(tmp_path)
    base = script_dir / "prompts" / "architecture-reviewer.md"
    base.write_text("## arch base\n")
    path, degraded = effective_prompt(
        "architecture-reviewer", base, script_dir, enable_suggestions=True
    )
    content = path.read_text()
    assert "findings trailer" in content
    assert "suggestion addendum" not in content
    # architecture-reviewer isn't in the suggestion set, so missing addendum
    # doesn't count as degraded
    assert degraded is False


def test_effective_prompt_degraded_when_addendum_missing(tmp_path: Path) -> None:
    """Missing suggestion-addendum sets degraded=True for eligible agents."""
    script_dir, base = _make_prompt_dir(tmp_path)
    (script_dir / "prompts" / "suggestion-addendum.md").unlink()
    path, degraded = effective_prompt(
        "code-reviewer", base, script_dir, enable_suggestions=True
    )
    assert degraded is True
    content = path.read_text()
    assert "suggestion addendum" not in content


def test_effective_prompt_missing_trailer_raises(tmp_path: Path) -> None:
    """Missing findings trailer raises FileNotFoundError — it's a required file."""
    script_dir, base = _make_prompt_dir(tmp_path)
    (script_dir / "prompts" / "_trailer-findings.md").unlink()
    with pytest.raises(FileNotFoundError, match="findings trailer"):
        effective_prompt("code-reviewer", base, script_dir, enable_suggestions=False)


def test_effective_prompt_missing_cutoff_raises(tmp_path: Path) -> None:
    """Missing knowledge-cutoff fragment raises FileNotFoundError — it's a required file."""
    script_dir, base = _make_prompt_dir(tmp_path)
    (script_dir / "prompts" / "_knowledge-cutoff.md").unlink()
    with pytest.raises(FileNotFoundError, match="knowledge-cutoff"):
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


def test_cache_priming_warns_on_unsupported_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Truthy priming with an unsupported provider should warn, not silently skip
    assert cache_priming_effective("antrhopic", "true", "auto") is False
    captured = capsys.readouterr()
    assert "cache priming requested" in captured.err
    assert "antrhopic" in captured.err


def test_cache_priming_silent_when_priming_disabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Priming not requested → no warning even on unsupported provider
    assert cache_priming_effective("openai", "false", "auto") is False
    captured = capsys.readouterr()
    assert captured.err == ""
