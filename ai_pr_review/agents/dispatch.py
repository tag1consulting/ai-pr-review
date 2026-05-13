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
    prompt_degraded: bool = False
    """True when a non-fatal prompt fragment was missing (e.g.
    suggestion-addendum.md). The agent still ran to completion, but its
    prompt was incomplete — callers may want to surface this to the user
    or exclude the output from downstream consumers that require the
    missing capability."""


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

    def __post_init__(self) -> None:
        if not self.standard_model:
            raise ValueError(
                "DispatchContext.standard_model must be non-empty "
                "(empty premium_model is acceptable; dispatch falls back to standard)"
            )


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
) -> tuple[Path, bool]:
    """Compose the effective prompt path and a degraded-flag for an agent.

    Returns ``(path, degraded)``. ``degraded=True`` signals that a non-fatal
    prompt fragment was skipped — currently only the suggestion-addendum. The
    agent can still run; callers may want to surface this to operators.

    Raises ``FileNotFoundError`` when a required fragment is missing (base
    prompt, knowledge-cutoff, or findings trailer).

    Tempfile ownership: when the returned path differs from ``base_prompt_path``,
    it is a caller-owned tempfile created under ``/tmp``. The caller is
    responsible for unlinking it after use. Inside ``run_tier`` this is handled
    by the ``finally`` block in ``_run_single_agent``; external callers must
    perform their own cleanup (``Path(p).unlink(missing_ok=True)``) to avoid
    leaks.
    """
    if agent_name not in _AGENTS_WITH_FINDINGS_TRAILER:
        return base_prompt_path, False

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

    if not cutoff_path.exists():
        raise FileNotFoundError(
            f"knowledge-cutoff fragment not found: {cutoff_path}; "
            "this file is required for agents that produce findings"
        )

    parts = [
        base_prompt_path.read_text(),
        cutoff_path.read_text(),
        trailer_path.read_text(),
    ]

    degraded = False
    if enable_suggestions and agent_name in _AGENTS_WITH_SUGGESTION_ADDENDUM:
        if suggestion_path.exists():
            parts.append(suggestion_path.read_text())
        else:
            degraded = True
            print(
                f"WARNING: suggestion-addendum fragment missing at {suggestion_path}; "
                f"agent '{agent_name}' will run without suggestion instructions",
                file=sys.stderr,
            )

    fd, tmp_path = tempfile.mkstemp(
        suffix=".md",
        prefix=f"effective-prompt-{agent_name}-",
    )
    try:
        with open(fd, "w") as fh:
            fh.write("\n".join(parts))
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink(missing_ok=True)
        raise
    return Path(tmp_path), degraded


# ---------------------------------------------------------------------------
# Cache priming
# ---------------------------------------------------------------------------

def cache_priming_effective(
    provider: str,
    cache_priming_env: str,
    prompt_caching_env: str,
) -> bool:
    """Return True only when conditions for cache priming are all met.

    Logs a warning to stderr when cache priming is requested
    (``cache_priming_env`` is truthy) but the provider does not support it —
    this surfaces typos in ``AI_PROVIDER`` (e.g. ``"antrhopic"``) that would
    otherwise silently disable priming.
    """
    if cache_priming_env.lower() not in ("true", "1"):
        return False
    if provider not in ("anthropic", "bedrock-proxy"):
        print(
            f"WARNING: cache priming requested but provider {provider!r} does not "
            "support it (expected 'anthropic' or 'bedrock-proxy'); "
            "cache priming will not run",
            file=sys.stderr,
        )
        return False
    return prompt_caching_env.lower() not in ("false", "0")


def _format_exception_chain(exc: BaseException) -> str:
    """Render an exception and its __cause__/__context__ chain into one string.

    FailedAgent.reason stores this so root-cause context is preserved when an
    agent wraps an upstream error (e.g. `httpx.HTTPStatusError` raised from
    inside a custom `RuntimeError`).
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        # __cause__ (explicit `raise X from Y`) takes precedence over __context__
        # (implicit during-handling chain). Only follow __context__ when
        # __suppress_context__ is False (the default).
        nxt = current.__cause__
        if nxt is None and not getattr(current, "__suppress_context__", False):
            nxt = current.__context__
        current = nxt
    return " | caused by ".join(parts)


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
        # pr-summarizer composes its own prompt + user message (manifest +
        # commit log + diff) via ai_pr_review.agents.summarizer.*. The generic
        # dispatch path here only passes the raw diff as user_message, which
        # would silently degrade summarizer output. Callers dispatching
        # pr-summarizer must use the summarizer module directly; until a
        # per-agent builder hook lands on AgentSpec, refuse the mismatch.
        if spec.name == "pr-summarizer":
            raise RuntimeError(
                "pr-summarizer must not be dispatched via run_tier; "
                "use ai_pr_review.agents.summarizer.build_summarizer_* helpers "
                "to compose prompt and user message, then call llm_call directly"
            )

        base_path = context.script_dir / spec.prompt_path
        prompt_path, prompt_degraded = effective_prompt(
            spec.name,
            base_path,
            context.script_dir,
            context.enable_suggestions,
        )
        if prompt_path != base_path:
            tmp_prompt = prompt_path

        use_premium = spec.tier == 2 and context.mode == "full" and bool(context.premium_model)
        model_id = context.premium_model if use_premium else context.standard_model
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
            prompt_degraded=prompt_degraded,
        ))
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        results.append(FailedAgent(
            name=spec.name,
            reason=_format_exception_chain(exc),
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
