"""Agent dispatch and parallelism layer."""

from __future__ import annotations

import contextlib
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import anyio

from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.llm.base import LLMRequest, LLMResponse

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenUsage:
    input: int
    output: int
    cache_creation: int
    cache_read: int
    model: str


@dataclass(frozen=True)
class AgentResult:
    name: str
    output: str
    token_log: TokenUsage | None
    truncated: bool


@dataclass(frozen=True)
class FailedAgent:
    name: str
    reason: str
    exit_code: int
    elapsed_ms: int


@dataclass
class DispatchContext:
    script_dir: Path
    mode: str
    diff_path: Path
    provider: str
    standard_model: str = ""
    premium_model: str = ""
    enable_suggestions: bool = True
    cache_priming_env: str = "false"
    prompt_caching_env: str = "auto"


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------

_AGENTS_WITH_FINDINGS_TRAILER: frozenset[str] = frozenset({
    "code-reviewer",
    "silent-failure-hunter",
    "security-reviewer",
    "edge-case-hunter",
    "blind-hunter",
    "architecture-reviewer",
    "adversarial-general",
})

_AGENTS_WITH_SUGGESTION_ADDENDUM: frozenset[str] = frozenset({
    "code-reviewer",
    "edge-case-hunter",
    "security-reviewer",
    "silent-failure-hunter",
    "blind-hunter",
})


def effective_prompt(
    agent_name: str,
    base_prompt_path: Path,
    script_dir: Path,
    enable_suggestions: bool,
) -> Path:
    """Return the effective prompt path for an agent.

    Finding-producing agents get knowledge-cutoff + findings trailer appended.
    pr-summarizer passes through unchanged.
    Raises FileNotFoundError if a required file (base or findings trailer) is missing.
    """
    if agent_name not in _AGENTS_WITH_FINDINGS_TRAILER:
        return base_prompt_path

    if not base_prompt_path.exists():
        raise FileNotFoundError(
            f"base prompt not found for agent '{agent_name}': {base_prompt_path}"
        )

    prompts_dir = script_dir / "prompts"
    cutoff_path = prompts_dir / "_knowledge-cutoff.md"
    trailer_path = prompts_dir / "_trailer-findings.md"
    suggestion_path = prompts_dir / "suggestion-addendum.md"

    if not trailer_path.exists():
        raise FileNotFoundError(
            f"findings trailer not found: {trailer_path}; "
            "this file is required for agents that emit json-findings blocks"
        )

    parts = [base_prompt_path.read_text()]

    if cutoff_path.exists():
        parts.append(cutoff_path.read_text())
    else:
        print(
            f"WARNING: knowledge-cutoff fragment not found: {cutoff_path}; "
            "agents may incorrectly flag recently-released versions",
            file=sys.stderr,
        )

    parts.append(trailer_path.read_text())

    if (
        enable_suggestions
        and agent_name in _AGENTS_WITH_SUGGESTION_ADDENDUM
        and suggestion_path.exists()
    ):
        parts.append(suggestion_path.read_text())

    fd, tmp_path = tempfile.mkstemp(
        suffix=".md",
        prefix=f"effective-prompt-{agent_name}-",
    )
    with open(fd, "w") as fh:
        fh.write("\n".join(parts))
    return Path(tmp_path)


# ---------------------------------------------------------------------------
# Cache priming
# ---------------------------------------------------------------------------

def cache_priming_effective(
    provider: str,
    cache_priming_env: str,
    prompt_caching_env: str,
) -> bool:
    """Return True only when conditions for cache priming are all met."""
    if cache_priming_env.lower() not in ("true", "1"):
        return False
    if provider not in ("anthropic", "bedrock-proxy"):
        return False
    return prompt_caching_env.lower() not in ("false", "0")


# ---------------------------------------------------------------------------
# Tier runner
# ---------------------------------------------------------------------------

LLMCall = Callable[[LLMRequest], Awaitable[LLMResponse]]


async def _run_single_agent(
    spec: AgentSpec,
    llm_call: LLMCall,
    context: DispatchContext,
    limiter: anyio.abc.CapacityLimiter,
    diff_text: str,
    results: list[AgentResult | FailedAgent],
) -> None:
    start = time.monotonic()
    tmp_prompt: Path | None = None
    try:
        base_path = context.script_dir / spec.prompt_path
        prompt_path = effective_prompt(
            spec.name,
            base_path,
            context.script_dir,
            context.enable_suggestions,
        )
        # Track temp files created by effective_prompt for cleanup.
        if prompt_path != base_path:
            tmp_prompt = prompt_path

        model_id = context.standard_model
        request = LLMRequest(
            model_id=model_id,
            system_prompt=prompt_path.read_text(),
            user_message=diff_text,
            max_tokens=spec.max_output_tokens,
        )
        async with limiter:
            response = await llm_call(request)
        usage = TokenUsage(
            input=response.input_tokens,
            output=response.output_tokens,
            cache_creation=response.cache_creation_tokens,
            cache_read=response.cache_read_tokens,
            model=model_id,
        )
        truncated = response.stop_reason in ("max_tokens", "length", "MAX_TOKENS")
        results.append(AgentResult(
            name=spec.name,
            output=response.text,
            token_log=usage,
            truncated=truncated,
        ))
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        print(
            f"ERROR: agent '{spec.name}' failed after {elapsed}ms: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        results.append(FailedAgent(
            name=spec.name,
            reason=str(exc),
            exit_code=1,
            elapsed_ms=elapsed,
        ))
    finally:
        if tmp_prompt is not None:
            with contextlib.suppress(OSError):
                tmp_prompt.unlink(missing_ok=True)


async def run_tier(
    agents: list[AgentSpec],
    llm_call: LLMCall,
    context: DispatchContext,
    semaphore_size: int,
) -> tuple[list[AgentResult], list[FailedAgent]]:
    """Run a tier of agents concurrently, returning (successes, failures)."""
    try:
        diff_text = context.diff_path.read_text()
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read diff file '{context.diff_path}': {exc}"
        ) from exc

    limiter = anyio.CapacityLimiter(semaphore_size)
    results: list[AgentResult | FailedAgent] = []

    async with anyio.create_task_group() as tg:
        for spec in agents:
            tg.start_soon(
                _run_single_agent, spec, llm_call, context, limiter, diff_text, results
            )

    successes: list[AgentResult] = []
    failures: list[FailedAgent] = []
    for outcome in results:
        if isinstance(outcome, AgentResult):
            successes.append(outcome)
        else:
            failures.append(outcome)

    for failure in failures:
        print(
            f"WARNING: agent '{failure.name}' failed "
            f"(exit_code={failure.exit_code}, elapsed={failure.elapsed_ms}ms): "
            f"{failure.reason}",
            file=sys.stderr,
        )

    return successes, failures
