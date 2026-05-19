"""Agent dispatch and parallelism layer."""

from __future__ import annotations

import contextlib
import logging
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.language_profiles import load_language_profiles
from ai_pr_review.languages import detect_language
from ai_pr_review.llm.base import LLMRequest, LLMResponse

_log = logging.getLogger(__name__)

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
    context_tokens_used: int = 0
    """Token count of the <symbol-context> block prepended for this agent.
    Zero when context enrichment was disabled or produced no context.
    E4.S3: used by the CLI to populate the Context enrichment row in the
    token cost table."""
    elapsed_ms: int = 0
    """Wall-clock milliseconds from call start to response received.
    E4.S4: used by cli.py to populate agent_latency_ms in TelemetryEvent."""


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
    # --- Context enrichment ---
    enable_context_enrichment: bool = False
    context_max_tokens: int = 8192
    context_lookup_lines: int = 8
    repo_root: Path = field(default_factory=Path.cwd)
    changed_files: list[str] = field(default_factory=list)
    # --- Feedback loop ---
    feedback_addendum: str = ""
    # #316: user-configurable per-agent output cap (0 = use roster default)
    max_tokens_per_agent: int = 0

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
                f"\n[ai-pr-review] WARNING: suggestion-addendum fragment missing at {suggestion_path}; "
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

    If the chain is cyclic or deeper than 16 links, an explicit marker is
    appended so callers can distinguish a truncated chain from a naturally
    terminated one.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    max_depth = 16
    truncated_reason: str | None = None
    while current is not None:
        if id(current) in seen:
            truncated_reason = "<cycle detected>"
            break
        if len(parts) >= max_depth:
            truncated_reason = f"<truncated after {max_depth} links>"
            break
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        # __cause__ (explicit `raise X from Y`) takes precedence over __context__
        # (implicit during-handling chain). Only follow __context__ when
        # __suppress_context__ is False (the default).
        nxt = current.__cause__
        if nxt is None and not getattr(current, "__suppress_context__", False):
            nxt = current.__context__
        current = nxt
    rendered = " | caused by ".join(parts)
    if truncated_reason is not None:
        rendered += f" | {truncated_reason}"
    return rendered


# ---------------------------------------------------------------------------
# Tier runner
# ---------------------------------------------------------------------------

LLMCall = Callable[[LLMRequest], Awaitable[LLMResponse]]


def _build_user_message(
    diff_text: str,
    spec: AgentSpec,
    context: DispatchContext,
) -> tuple[str, int]:
    """Build the user message for an agent, optionally prepending symbol context.

    Returns ``(message, context_tokens_used)``.  When context enrichment is
    enabled and the agent is eligible, extracts symbol references from the diff,
    looks up their definitions via ripgrep, and prepends a ``<symbol-context>``
    block.  Falls back to raw diff on any error (fail-soft).
    """
    if not (context.enable_context_enrichment and spec.context_enrichment_eligible):
        return diff_text, 0

    try:
        from ai_pr_review.context.budget import build_context_block, estimate_tokens
        from ai_pr_review.context.symbols import lookup_definitions
        from ai_pr_review.context.treesitter import extract_symbol_refs

        # Detect language from changed_files (use the most common extension)
        language = _detect_primary_language(context.changed_files)
        refs = extract_symbol_refs(diff_text, language)
        if not refs:
            return diff_text, 0

        defs = lookup_definitions(
            refs,
            context.repo_root,
            context.changed_files,
            language=language,
            lookup_lines=context.context_lookup_lines,
        )
        ctx_block = build_context_block(defs, max_tokens=context.context_max_tokens)
        if ctx_block:
            ctx_tokens = estimate_tokens(ctx_block)
            _log.info(
                "context enrichment: agent=%s refs=%d defs=%d block≈%d tokens",
                spec.name, len(refs), len(defs), ctx_tokens,
            )
            return ctx_block + "\n\n" + diff_text, ctx_tokens
    except Exception as exc:
        # exc_info=True so unexpected errors (MemoryError, bugs in
        # build_context_block, etc.) are distinguishable from expected
        # failures (missing grammar, ripgrep absent) in production logs.
        _log.warning(
            "context enrichment failed for agent %r: %s",
            spec.name, exc, exc_info=True,
        )

    return diff_text, 0


def _unique_language_labels(changed_files: list[str]) -> list[str]:
    """Return ordered unique language labels for changed_files.

    Iterates files in order, emitting each label the first time it appears.
    Uses detect_language() from ai_pr_review.languages so the mapping stays
    in sync with the single source of truth (_EXT_MAP).
    """
    seen: set[str] = set()
    labels: list[str] = []
    for f in changed_files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        label = detect_language(ext)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _detect_primary_language(changed_files: list[str]) -> str:
    """Return the most common language key from a list of file paths.

    Delegates to detect_language() so the extension mapping stays in sync
    with the single source of truth (_EXT_MAP in ai_pr_review.languages).
    Returns a lowercase label for tree-sitter grammar selection.
    """
    counts: dict[str, int] = {}
    for f in changed_files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        label = detect_language(ext)
        if label:
            lang = label.lower()
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda k: counts[k])


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

        # Build user message (includes context enrichment when enabled).
        user_message, context_tokens_used = _build_user_message(diff_text, spec, context)

        # Inject feedback addendum into system prompt when available.
        system_prompt = prompt_path.read_text()
        if context.feedback_addendum:
            system_prompt = system_prompt + "\n\n" + context.feedback_addendum

        # Inject language profile context (mirrors lib/diff.sh:297-306).
        lang_labels = _unique_language_labels(context.changed_files)
        lang_context = load_language_profiles(lang_labels, context.script_dir)
        if lang_context:
            system_prompt = system_prompt + "\n\n" + lang_context

        # #316: honour AI_MAX_TOKENS_PER_AGENT when set; fall back to roster default
        max_tokens = (
            context.max_tokens_per_agent
            if context.max_tokens_per_agent > 0
            else spec.max_output_tokens
        )
        request = LLMRequest(
            model_id=model_id,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
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
        elapsed = int((time.monotonic() - start) * 1000)
        results.append(AgentResult(
            name=spec.name,
            output=response.text,
            token_log=usage,
            truncated=truncated,
            prompt_degraded=prompt_degraded,
            context_tokens_used=context_tokens_used,
            elapsed_ms=elapsed,
        ))
    except BaseException as exc:
        # Catch BaseException (not Exception) so SystemExit from llm/client.py
        # — raised on auth failure (exit 1), retry exhaustion (exit 2), or
        # content-filter block (exit 3) — is isolated to a single FailedAgent
        # instead of cancelling sibling tasks in the anyio task group.
        # KeyboardInterrupt is re-raised so Ctrl-C still aborts the run.
        if isinstance(exc, KeyboardInterrupt):
            raise
        elapsed = int((time.monotonic() - start) * 1000)
        exit_code = 1
        if isinstance(exc, SystemExit) and isinstance(exc.code, int):
            exit_code = exc.code
        results.append(FailedAgent(
            name=spec.name,
            reason=_format_exception_chain(exc),
            exit_code=exit_code,
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
            f"\n[ai-pr-review] WARNING: agent '{failure.name}' failed "
            f"(exit_code={failure.exit_code}, elapsed={failure.elapsed_ms}ms): "
            f"{failure.reason}",
            file=sys.stderr,
        )

    return successes, failures
