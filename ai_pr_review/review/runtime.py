"""Review runtime assembly: builds fully-prepared inputs for orchestrate.run_review().

All env reads, provider construction, compute, and dependency wiring live here.
`build_review_runtime()` is the boundary between env-driven configuration and
pure orchestration. `orchestrate.run_review()` receives only the assembled
`ReviewRuntime` and constructs no dependencies of its own.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ai_pr_review.agents.dispatch import (
    DispatchContext,
    _unique_language_labels,
    load_shared_prompt_fragments,
)
from ai_pr_review.config import ReviewConfig
from ai_pr_review.language_profiles import load_language_profiles
from ai_pr_review.manifest import ChangedFiles, parse_changed_files_payload
from ai_pr_review.orchestrate import OrchestrationConfig
from ai_pr_review.review.compute import run_compute
from ai_pr_review.vcs import provider_from_env
from ai_pr_review.vcs.protocol import DiffContext, VcsProvider

if TYPE_CHECKING:
    from ai_pr_review.agents.roster import AgentSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkipPlan:
    """Compute returned skip — caller posts a skip comment and returns 0."""

    reason: str
    provider: VcsProvider


@dataclass(frozen=True)
class ReviewRuntime:
    """Fully prepared inputs for orchestrate.run_review() and the CLI layer.

    All fields are pre-built. The orchestrator reads no environment and
    constructs no dependencies.

    `config` is the post-resolve_models() copy; use it (not the original) for
    model names, telemetry, and the summarizer call.
    """

    config: ReviewConfig
    provider: VcsProvider
    diff: DiffContext
    # Prefix portion of summary_text (merge-filter note); CLI appends summarizer output.
    summary_prefix: str
    is_incremental: bool
    manifest_text: str
    base_ref: str
    head_sha: str
    agents: tuple[AgentSpec, ...]
    changed_files: ChangedFiles
    dispatch_context: DispatchContext
    orch_config: OrchestrationConfig
    # script_dir exposed so CLI can build the token-table renderer closure.
    script_dir: Path
    diff_path: Path
    feedback_entries_count: int
    sarif_elapsed_s: float | None


async def build_review_runtime(
    config: ReviewConfig,
    *,
    provider_factory: Callable[[], VcsProvider] | None = None,
) -> ReviewRuntime | SkipPlan:
    """Assemble all prepared inputs for run_review().

    Reads env only for SCRIPT_DIR / DIFF_FILE conventions. All other
    configuration flows through the validated ReviewConfig. Pure of Click
    and exit-code concerns.

    The `provider_factory` seam lets tests inject a fake VcsProvider without
    requiring env vars. Defaults to `provider_from_env`.
    """
    from ai_pr_review.agents.gates import evaluate_gates, filter_agents
    from ai_pr_review.agents.roster import AGENTS, agent_allowed
    from ai_pr_review.findings.models import Finding as _Finding

    _factory = provider_factory if provider_factory is not None else provider_from_env

    # Resolve provider model defaults before any downstream use.
    config = config.resolve_models()

    # 1. Build provider.
    provider = _factory()
    if not isinstance(provider, VcsProvider):
        raise TypeError(f"Expected VcsProvider, got {type(provider).__name__}")

    # 2. Fetch last-reviewed SHA *before* compute so incremental diff works.
    last_reviewed = provider.get_last_reviewed_sha()

    # 3. Run compute phase (incremental when SHA available).
    payload = run_compute(config, last_reviewed_sha=last_reviewed)
    if payload.get("skip"):
        reason = str(payload.get("reason") or "no changes")
        return SkipPlan(reason=reason, provider=provider)

    diff_text = str(payload.get("diff") or "")
    head_sha = str(payload.get("head") or config.head_sha)
    base_ref = str(payload.get("base") or config.base_ref)
    manifest_text = str(payload.get("manifest") or "")
    is_incremental = bool(payload.get("is_incremental"))

    # 4. Build summary prefix — prepend merge-filter fallback note when present.
    merge_filter_fallback = str(payload.get("merge_filter_fallback_reason") or "")
    summary_prefix = ""
    if merge_filter_fallback:
        summary_prefix = (
            f"_Note: merge-commit filtering was skipped ({merge_filter_fallback}); "
            "diff may include upstream changes._\n\n"
        )

    # 5. Build feedback addendum when AI_FEEDBACK_LOOP=1 (before dispatch context).
    feedback_addendum = ""
    feedback_entries_count = 0
    if config.enable_feedback_loop:
        try:
            from ai_pr_review.feedback.inject import build_feedback_addendum
            from ai_pr_review.feedback.store import make_store
            store = make_store(config)
            entries = store.load_recent()
            feedback_entries_count = len(entries)
            feedback_addendum = build_feedback_addendum(
                entries, diff_text, max_tokens=config.feedback_max_tokens
            )
        except ImportError:
            raise
        except Exception as exc:
            logger.warning(
                "feedback loop: could not load feedback store: %s", exc, exc_info=True
            )

    # 6. Resolve script_dir / diff_path from env conventions.
    # AI_PR_REVIEW_SCRIPT_DIR is exported by review.sh so the Python engine
    # can locate prompts/language-profiles when installed as a pip package.
    _env_script_dir = os.environ.get("AI_PR_REVIEW_SCRIPT_DIR")
    script_dir = (
        Path(_env_script_dir) if _env_script_dir
        else Path(__file__).resolve().parent.parent.parent
    )
    diff_path = Path(os.environ.get("AI_PR_REVIEW_DIFF_FILE") or "/tmp/ai-review-diff.txt")
    try:
        diff_path.write_text(diff_text, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Failed to write diff staging file {diff_path}: {exc}. "
            "Check AI_PR_REVIEW_DIFF_FILE or /tmp permissions."
        ) from exc

    raw_paths: list[object] = (
        payload.get("changed_files")  # type: ignore[assignment]
        if isinstance(payload.get("changed_files"), list)
        else []
    )
    cf = parse_changed_files_payload(raw_paths)
    _changed_list = cf.all_files

    # 7. Load language profiles once for the whole run (avoid per-agent disk reads).
    _lang_labels = _unique_language_labels(_changed_list)
    _language_profile_text = load_language_profiles(_lang_labels, script_dir)

    # Load shared prompt fragments once per run to avoid per-agent disk reads
    # inside effective_prompt().  When the prompts directory is absent (e.g. in
    # tests that use a fake script_dir), skip the load — effective_prompt() will
    # fall back to reading from disk on first use, raising FileNotFoundError only
    # if a finding-producing agent actually dispatches.
    _prompts_dir = script_dir / "prompts"
    _shared_fragments = (
        load_shared_prompt_fragments(script_dir, enable_suggestions=config.enable_suggestions)
        if _prompts_dir.is_dir()
        else None
    )

    # 8. Build dispatch context.
    dispatch_ctx = DispatchContext(
        script_dir=script_dir,
        mode=config.review_mode,
        diff_path=diff_path,
        provider=config.provider,
        standard_model=config.model_standard,
        premium_model=config.model_premium,
        enable_suggestions=config.enable_suggestions,
        cache_priming_env="true" if config.cache_priming else "false",
        prompt_caching_env=config.llm_prompt_caching,
        enable_context_enrichment=config.enable_context_enrichment,
        context_max_tokens=config.context_max_tokens,
        context_lookup_lines=config.context_lookup_lines,
        repo_root=Path("."),
        changed_files=_changed_list,
        feedback_addendum=feedback_addendum,
        max_tokens_per_agent=config.max_tokens_per_agent,
        temperature=config.temperature,
        language_profile_text=_language_profile_text,
        _shared_prompt_fragments=_shared_fragments,
    )

    # 9. Evaluate gates and filter agent roster.
    gates = evaluate_gates(
        diff_text=diff_text,
        changed_files=cf,
        env=os.environ,
        last_reviewed_sha=last_reviewed,
    )
    mode_filtered = [
        a for a in AGENTS if not a.full_mode_only or config.review_mode == "full"
    ]
    # Agents marked separately_dispatched run via their own code paths; exclude from generic dispatch.
    mode_filtered = [a for a in mode_filtered if not a.separately_dispatched]
    # Apply allowlist/denylist narrowing before gate evaluation so gates still apply on top.
    # Empty allow + empty deny => no filtering (all agents permitted).
    mode_filtered = [
        a for a in mode_filtered
        if agent_allowed(a.name, config.agents, config.exclude_agents)
    ]
    agents = filter_agents(mode_filtered, gates)
    if not agents:
        logger.warning(
            "review proceeding with 0 agents after gate filtering "
            "(gates=%s); only pre-computed findings will be posted",
            list(gates),
        )

    # 10. Run native static analyzers — fail-soft; findings merged via extra_findings.
    analyzer_findings: list[_Finding] = []
    try:
        from ai_pr_review.analyzers.bridge import (
            ANALYZER_NAMES,
            _analyzer_skip_names,
            _sarif_covered_names,
            run_analyzers,
        )
        _disabled = _analyzer_skip_names(config.analyzers, config.exclude_analyzers)
        if _disabled >= ANALYZER_NAMES:
            logger.warning(
                "analyzers: allow/deny configuration disables all known analyzers; "
                "no static analysis will run"
            )
        analyzer_findings = await run_analyzers(
            cf,
            diff_file=str(diff_path),
            script_dir=str(script_dir),
            concurrency=config.analyzer_concurrency,
            sarif_skip=_sarif_covered_names(config.sarif_paths),
            disabled=_disabled,
        )
        if analyzer_findings:
            logger.info(
                "analyzers: %d finding(s) from native static analysis",
                len(analyzer_findings),
            )
    except ImportError:
        raise
    except Exception as exc:
        logger.warning(
            "analyzers: static analyzer run failed (fail-soft): %s", exc, exc_info=True
        )

    # 11. Load SARIF findings and merge with analyzer findings into extra_findings.
    sarif_findings: list[_Finding] = []
    sarif_elapsed_s: float | None = None
    if config.sarif_paths:
        sarif_paths = list(config.sarif_paths)
        try:
            from ai_pr_review.analyzers.sarif import load_sarif_files
            sarif_raw, sarif_elapsed_s = load_sarif_files(sarif_paths)
            sarif_findings = [f for f in sarif_raw if isinstance(f, _Finding)]
            dropped = len(sarif_raw) - len(sarif_findings)
            if dropped:
                logger.warning(
                    "SARIF: dropped %d non-Finding entries (schema mismatch in %s)",
                    dropped, sarif_paths,
                )
            if sarif_findings:
                logger.info("SARIF: loaded %d finding(s)", len(sarif_findings))
        except Exception as exc:
            logger.warning(
                "SARIF: load failed (fail-soft) for %s: %s",
                sarif_paths, exc, exc_info=True,
            )

    extra_findings = tuple(analyzer_findings) + tuple(sarif_findings)

    # 12. Load global + local suppression rules (fail-soft — malformed rules file
    # must not abort the review; proceed with no suppressions and log a warning).
    from ai_pr_review.findings.suppress import load_rules as _load_suppression_rules
    try:
        suppression_rules = tuple(
            _load_suppression_rules(str(script_dir), workspace=".")
        )
    except Exception as exc:
        logger.warning(
            "suppressions: could not load rules (proceeding without): %s", exc, exc_info=True
        )
        suppression_rules = ()

    # 13. Build orchestrator config.
    orch_config = OrchestrationConfig(
        mode=config.review_mode,  # type: ignore[arg-type]
        confidence_threshold=config.confidence_threshold,
        max_inline=config.max_inline,
        enable_suggestions=config.enable_suggestions,
        semaphore_size=config.concurrency,
        suppression_rules=suppression_rules,
        extra_findings=extra_findings,
        analyzer_diff_scope=config.analyzer_diff_scope,
    )

    return ReviewRuntime(
        config=config,
        provider=provider,
        diff=DiffContext(diff_text=diff_text, head_sha=head_sha),
        summary_prefix=summary_prefix,
        is_incremental=is_incremental,
        manifest_text=manifest_text,
        base_ref=base_ref,
        head_sha=head_sha,
        agents=tuple(agents),
        changed_files=cf,
        dispatch_context=dispatch_ctx,
        orch_config=orch_config,
        script_dir=script_dir,
        diff_path=diff_path,
        feedback_entries_count=feedback_entries_count,
        sarif_elapsed_s=sarif_elapsed_s,
    )
