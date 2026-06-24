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
    profile_tokens_used: int = 0
    """Token count of the routed language-profile section prepended for this agent.
    Zero when profile routing was disabled or produced no text.
    Story 7-2: used by the CLI to populate the Language profiles row in the
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


@dataclass(frozen=True)
class _SharedPromptFragments:
    """Shared prompt fragments loaded once per run and threaded via DispatchContext.

    All three are required for finding-producing agents; suggestion_addendum is
    optional (empty string when the file is absent).
    """

    governance: str
    knowledge_cutoff: str
    findings_trailer: str
    suggestion_addendum: str  # "" when file absent or suggestions disabled


def load_shared_prompt_fragments(
    script_dir: Path,
    enable_suggestions: bool = True,
) -> _SharedPromptFragments:
    """Load the four shared prompt fragments from disk once per run.

    Raises FileNotFoundError when a required fragment is missing (governance,
    knowledge-cutoff, or findings trailer). Returns a _SharedPromptFragments
    with suggestion_addendum="" when the suggestion file is absent or
    enable_suggestions is False.
    """
    prompts_dir = script_dir / "prompts"
    governance_path = prompts_dir / "_governance.md"
    cutoff_path = prompts_dir / "_knowledge-cutoff.md"
    trailer_path = prompts_dir / "_trailer-findings.md"
    suggestion_path = prompts_dir / "suggestion-addendum.md"

    if not governance_path.exists():
        raise FileNotFoundError(
            f"governance fragment not found: {governance_path}; "
            "this file is required for agents that produce findings"
        )
    if not cutoff_path.exists():
        raise FileNotFoundError(
            f"knowledge-cutoff fragment not found: {cutoff_path}; "
            "this file is required for agents that produce findings"
        )
    if not trailer_path.exists():
        raise FileNotFoundError(
            f"findings trailer not found: {trailer_path}; "
            "this file is required for agents that emit json-findings blocks"
        )

    suggestion_text = ""
    if enable_suggestions and suggestion_path.exists():
        suggestion_text = suggestion_path.read_text()

    return _SharedPromptFragments(
        governance=governance_path.read_text(),
        knowledge_cutoff=cutoff_path.read_text(),
        findings_trailer=trailer_path.read_text(),
        suggestion_addendum=suggestion_text,
    )


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
    context_max_queries: int = 200
    repo_root: Path = field(default_factory=Path.cwd)
    changed_files: list[str] = field(default_factory=list)
    # --- Feedback loop ---
    feedback_addendum: str = ""
    # #316: user-configurable per-agent output cap (0 = use roster default)
    max_tokens_per_agent: int = 0
    # #356: user-configurable temperature for agent LLM calls
    temperature: float = 0.3
    # Per-agent language-profile router (Story 7-2, #355). Built once per run in
    # build_review_runtime() from all detected language profiles. None means no
    # profiles were loaded (e.g. non-code changes, or tests that don't set it).
    profile_router: object | None = field(default=None, repr=False)
    # Token cap applied per agent when assembling the routed profile section.
    profile_max_tokens: int = 4096
    # Pre-loaded shared prompt fragments (governance, knowledge-cutoff, trailer,
    # suggestion-addendum). None means not yet loaded; effective_prompt will read
    # them from disk the first time and cache them here. Callers that construct
    # DispatchContext directly (e.g. in tests) may leave this as None — the
    # first effective_prompt call will populate it transparently.
    _shared_prompt_fragments: _SharedPromptFragments | None = field(default=None, repr=False, compare=False)

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
    shared_fragments: _SharedPromptFragments | None = None,
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

    Performance: when ``shared_fragments`` is provided (pre-loaded once per run
    via ``load_shared_prompt_fragments``), the four shared files are read from
    memory rather than disk.  When it is None, they are read from disk as
    before (backward-compatible fallback used by tests and callers that
    construct DispatchContext without populating _shared_prompt_fragments).
    """
    if agent_name not in _AGENTS_WITH_FINDINGS_TRAILER:
        return base_prompt_path, False

    if not base_prompt_path.exists():
        raise FileNotFoundError(
            f"base prompt not found for agent '{agent_name}': {base_prompt_path}"
        )

    # Composition order: base → _governance → _knowledge-cutoff → _trailer-findings → (suggestion).
    # Governance leads the shared tail; cutoff/trailer/suggestion bytes stay
    # byte-identical to preserve prompt-cache locality.
    if shared_fragments is not None:
        # Fast path: use pre-loaded fragments (no disk I/O for shared files).
        parts = [
            base_prompt_path.read_text(),
            shared_fragments.governance,
            shared_fragments.knowledge_cutoff,
            shared_fragments.findings_trailer,
        ]
        degraded = False
        if enable_suggestions and agent_name in _AGENTS_WITH_SUGGESTION_ADDENDUM:
            if shared_fragments.suggestion_addendum:
                parts.append(shared_fragments.suggestion_addendum)
            else:
                degraded = True
                prompts_dir = script_dir / "prompts"
                suggestion_path = prompts_dir / "suggestion-addendum.md"
                print(
                    f"\n[ai-pr-review] WARNING: suggestion-addendum fragment missing at {suggestion_path}; "
                    f"agent '{agent_name}' will run without suggestion instructions",
                    file=sys.stderr,
                )
    else:
        # Fallback: read all fragments from disk (backward-compatible path for
        # tests and callers that do not pre-populate _shared_prompt_fragments).
        prompts_dir = script_dir / "prompts"
        governance_path = prompts_dir / "_governance.md"
        cutoff_path = prompts_dir / "_knowledge-cutoff.md"
        trailer_path = prompts_dir / "_trailer-findings.md"
        suggestion_path = prompts_dir / "suggestion-addendum.md"

        if not governance_path.exists():
            raise FileNotFoundError(
                f"governance fragment not found: {governance_path}; "
                "this file is required for agents that produce findings"
            )
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
            governance_path.read_text(),
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


@dataclass(frozen=True)
class _EnrichmentBlock:
    """Pre-computed context-enrichment payload, shared across all agents in a tier.

    Built once at the top of ``run_tier`` because every input
    (``diff_text``, ``changed_files``, ``repo_root``, ``language``,
    ``context_lookup_lines``, ``context_max_tokens``) is fixed for the run —
    every agent that opted into enrichment would otherwise reparse the diff
    and re-run the symbol lookup with identical inputs and identical outputs.
    """

    text: str
    tokens: int


def _compute_context_enrichment_block(
    diff_text: str,
    context: DispatchContext,
) -> _EnrichmentBlock | None:
    """Run the diff-parse + symbol-lookup + budget once for the whole tier.

    Returns ``None`` when enrichment is disabled or yields nothing useful;
    callers fall back to the raw diff. Errors are logged and swallowed so a
    bad parse never breaks the dispatch (fail-soft, matches per-agent legacy
    behavior).
    """
    if not context.enable_context_enrichment:
        return None
    try:
        from ai_pr_review.context.budget import build_context_block, estimate_tokens
        from ai_pr_review.context.symbols import lookup_definitions
        from ai_pr_review.context.treesitter import extract_symbol_refs

        language = _detect_primary_language(context.changed_files)
        refs = extract_symbol_refs(diff_text, language)
        if not refs:
            return None
        defs = lookup_definitions(
            refs,
            context.repo_root,
            context.changed_files,
            language=language,
            lookup_lines=context.context_lookup_lines,
            max_queries=context.context_max_queries,
        )
        ctx_text = build_context_block(defs, max_tokens=context.context_max_tokens)
        if not ctx_text:
            return None
        ctx_tokens = estimate_tokens(ctx_text)
        _log.info(
            "context enrichment: lang=%s refs=%d defs=%d block≈%d tokens "
            "(computed once for the tier)",
            language, len(refs), len(defs), ctx_tokens,
        )
        return _EnrichmentBlock(text=ctx_text, tokens=ctx_tokens)
    except Exception as exc:
        # exc_info=True so unexpected errors (MemoryError, bugs in
        # build_context_block, etc.) are distinguishable from expected
        # failures (missing grammar, ripgrep absent) in production logs.
        _log.warning("context enrichment failed: %s", exc, exc_info=True)
        return None


def _build_user_message(
    diff_text: str,
    spec: AgentSpec,
    context: DispatchContext,
    enrichment: _EnrichmentBlock | None = None,
) -> tuple[str, int]:
    """Build the user message for an agent.

    When ``enrichment`` is provided AND ``spec.context_enrichment_eligible`` is
    true, prepends the precomputed ``<symbol-context>`` block to the diff and
    returns the block's token count for telemetry. Otherwise returns the raw
    diff. The expensive parse + symbol lookup happens once per tier in
    ``_compute_context_enrichment_block``; this function is now a near-trivial
    formatter.
    """
    if enrichment is None or not spec.context_enrichment_eligible:
        return diff_text, 0
    return enrichment.text + "\n\n" + diff_text, enrichment.tokens


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
    enrichment: _EnrichmentBlock | None = None,
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
            shared_fragments=context._shared_prompt_fragments,
        )
        if prompt_path != base_path:
            tmp_prompt = prompt_path

        use_premium = spec.tier == 2 and context.mode == "full" and bool(context.premium_model)
        model_id = context.premium_model if use_premium else context.standard_model

        # Build user message. The expensive context-enrichment work (diff
        # parse + symbol lookup) ran once per tier in run_tier(); this just
        # prepends the precomputed block when the agent opted in.
        user_message, context_tokens_used = _build_user_message(
            diff_text, spec, context, enrichment=enrichment,
        )

        system_prompt = prompt_path.read_text()

        # Run-shared system tail: feedback addendum + language profiles. These
        # are byte-identical across every agent in a run, so they go into
        # LLMRequest.system_prefix where Anthropic/Bedrock can mark them with
        # a shared cache breakpoint and read them once across the whole run
        # instead of paying for them per-agent.  Providers without
        # multi-breakpoint caching concatenate them ahead of system_prompt,
        # preserving identical model-visible content.
        #
        # Language profiles are gated on context_enrichment_eligible: agents
        # like blind-hunter explicitly ask the model to reason about the diff
        # with no project context, so injecting language profiles would defeat
        # their purpose and waste tokens on content the model is told to ignore.
        # Feedback addenda are run-shared learning signals and reach all agents.
        #
        # Profile routing (Story 7-2): each agent receives only the profile
        # sections relevant to its profile_focus, packed under profile_max_tokens.
        # This makes system_prefix differ per agent, reducing cross-agent prompt-cache
        # reuse for the profile block — but delivers smaller, more relevant context.
        prefix_parts: list[str] = []
        profile_tokens_used = 0
        if context.feedback_addendum:
            prefix_parts.append(context.feedback_addendum)
        if spec.context_enrichment_eligible and context.profile_router is not None:
            from ai_pr_review.context.budget import estimate_tokens
            from ai_pr_review.language_profile_sections import ProfileRouter
            if isinstance(context.profile_router, ProfileRouter):
                routed_text = context.profile_router.route(
                    spec.profile_focus, context.profile_max_tokens
                )
                if routed_text:
                    prefix_parts.append(routed_text)
                    profile_tokens_used = estimate_tokens(routed_text)
        system_prefix = "\n\n".join(prefix_parts)

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
            temperature=context.temperature,
            system_prefix=system_prefix,
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
            profile_tokens_used=profile_tokens_used,
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

    # Compute context-enrichment once per tier so the diff parse + symbol
    # lookup don't repeat per agent (#499). Skipped entirely when no agent in
    # the tier opted in, even if enrichment is enabled at the run level.
    enrichment: _EnrichmentBlock | None = None
    if any(spec.context_enrichment_eligible for spec in agents):
        enrichment = _compute_context_enrichment_block(diff_text, context)

    limiter = anyio.CapacityLimiter(semaphore_size)
    results: list[AgentResult | FailedAgent] = []

    async with anyio.create_task_group() as tg:
        for spec in agents:
            tg.start_soon(
                _run_single_agent, spec, llm_call, context, limiter, diff_text, results,
                enrichment,
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
