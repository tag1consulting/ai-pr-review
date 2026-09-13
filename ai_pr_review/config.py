"""Typed configuration for the AI PR Review engine.

Maps every environment variable into a pydantic ReviewConfig. Unknown AI_*
vars raise ConfigError with a nearest-match suggestion.
"""

from __future__ import annotations

import difflib
import os
import sys

from pydantic import BaseModel, field_validator, model_validator


class ConfigError(ValueError):
    """Raised when an unknown AI_* variable is detected in the environment."""


# ---------------------------------------------------------------------------
# Canonical list of all documented AI_* variable names (for typo suggestion).
# ---------------------------------------------------------------------------
_KNOWN_AI_VARS: frozenset[str] = frozenset(
    {
        "AI_PROVIDER",
        "AI_MODEL_STANDARD",
        "AI_MODEL_PREMIUM",
        "AI_REVIEW_MODE",
        "AI_TEMPERATURE",
        "AI_PARALLEL",
        "AI_MAX_INLINE",
        "AI_MAX_TOKENS_PER_AGENT",
        "AI_ENABLE_SUGGESTIONS",
        "AI_CONFIDENCE_THRESHOLD",
        "AI_DISABLE_GATE_ARCHITECTURE",
        "AI_DISABLE_GATE_SECURITY",
        "AI_DISABLE_GATE_EDGE_CASE",
        "AI_DRY_RUN",
        "AI_IGNORE_MERGE_COMMITS",
        "AI_PR_REVIEW_RECORD_DIR",
        # Read directly by cli.py's `compute` subcommand (a `--output` click
        # option with this envvar as its Click default), not by Config --
        # ReviewConfig.compute_output was an unread duplicate field, removed
        # in #824. Kept in this set so Config.from_env's unknown-AI_*-var
        # check doesn't warn about it.
        "AI_PR_REVIEW_COMPUTE_OUTPUT",
        "AI_PR_REVIEW_SCRIPT_DIR",
        # Read directly by review/runtime.py, not by Config -- registered here
        # only so Config.from_env's unknown-AI_*-var check doesn't warn about
        # it. A container-internal staging path (defaults to /tmp inside the
        # container); not forwarded by container-action/action.yml for the
        # same reason as AI_PR_REVIEW_RECORD_DIR (#761).
        "AI_PR_REVIEW_DIFF_FILE",
        # Claude Code sets this in its agent environment; not a user-configured var.
        "AI_AGENT",
        # --- Context enrichment ---
        "AI_CONTEXT_ENRICHMENT",
        "AI_CONTEXT_MAX_TOKENS",
        "AI_CONTEXT_LOOKUP_LINES",
        "AI_CONTEXT_MAX_QUERIES",
        # --- SARIF ingestion ---
        "AI_SARIF_PATHS",
        # --- Diff exclude patterns ---
        "AI_EXCLUDE_PATTERNS",
        "AI_EXCLUDE_PATTERNS_MODE",
        # --- Analyzer and agent allow/deny selection ---
        "AI_ANALYZERS",
        "AI_EXCLUDE_ANALYZERS",
        "AI_AGENTS",
        "AI_EXCLUDE_AGENTS",
        # --- Analyzer diff scope and concurrency ---
        "AI_ANALYZER_DIFF_SCOPE",
        "AI_ANALYZER_CONCURRENCY",
        # --- Slash commands + feedback loop ---
        "AI_FEEDBACK_LOOP",
        "AI_FEEDBACK_BRANCH",
        "AI_FEEDBACK_MAX_TOKENS",
        "AI_FEEDBACK_RETENTION_COUNT",
        "AI_FEEDBACK_RETENTION_AGE_DAYS",
        # --- Judge pass ---
        "AI_JUDGE_PASS",
        # --- Fail-on-findings ---
        "AI_FAIL_ON_FINDINGS",
        # --- Token usage display (#758) ---
        "AI_TOKEN_USAGE_DISPLAY",
        "AI_TOKEN_USAGE_WARN_USD",
        # --- Structured logging ---
        "AI_LOG_FORMAT",
        "AI_LOG_LEVEL",
        # Set by the engine at startup and inherited by analyzer subprocesses;
        # not a user-configured input but must be known to avoid ConfigError.
        "AI_PR_REVIEW_CORRELATION_ID",
        # --- Telemetry ---
        "AI_TELEMETRY_ENABLED",
        "AI_TELEMETRY_SINK",
        # --- Canonical-review reuse (GitHub only) ---
        # Read directly by ai_pr_review.vcs.__init__._build_github_from_env,
        # not by Config -- registered here only so Config.from_env's unknown-
        # AI_*-var check doesn't warn about it.
        "AI_CANONICAL_REUSE",
        # --- Cross-run finding dedup (GitLab only, #710) ---
        # Read directly by ai_pr_review.vcs.__init__._build_gitlab_from_env,
        # not by Config -- registered here only so Config.from_env's unknown-
        # AI_*-var check doesn't warn about it. Deliberately a separate flag
        # from AI_CANONICAL_REUSE: GitLab has no canonical review, and coupling
        # two independently-risky features would block rolling either back
        # alone.
        "AI_GITLAB_CROSS_RUN_DEDUP",
    }
)


# Variables that are accepted without error but have no effect on the engine.
# A deprecation warning is printed so users know to use the canonical name.
_DEPRECATED_AI_VAR_ALIASES: dict[str, str] = {
    # GitHub Actions workflow uses AI_REVIEW_IGNORE_MERGE_COMMITS as a repo
    # variable name; the engine reads AI_IGNORE_MERGE_COMMITS.
    "AI_REVIEW_IGNORE_MERGE_COMMITS": "AI_IGNORE_MERGE_COMMITS",
    # Bash engine selection var removed in v2.0.0; accept silently so consumers
    # that still pass engine: python don't get a hard ConfigError during rollout.
    "AI_PR_REVIEW_ENGINE": "",
}

# Variables whose underlying feature was removed, but which stay accepted as a
# documented no-op with a deprecation warning naming the removal release,
# per Epic 9's "no breaking changes" acceptance criterion (#806). Distinct
# from _DEPRECATED_AI_VAR_ALIASES (which points at a still-live canonical
# replacement, or -- for the single "" case grandfathered before this dict
# existed -- a feature already fully removed in a past release): each entry
# here names the reason plus the upcoming release the var will actually be
# rejected in.
_DEPRECATED_NOOP_AI_VARS: dict[str, str] = {
    # Per-agent language-profile routing removed (#814): every eligible agent
    # now receives the whole detected-language profile(s) instead of a
    # per-agent routed subset, so there is no longer a per-agent profile
    # token budget to cap.
    "AI_PROFILE_MAX_TOKENS": "language-profile routing removed in #814",
    # cache_priming_effective() and DispatchContext.cache_priming_env were
    # deleted as dead code (zero production callers) in #807. That commit's
    # message claimed AI_CACHE_PRIMING would "stay accepted in config.py as a
    # documented no-op", but it was never actually added to this registry --
    # it stayed silently accepted via _KNOWN_AI_VARS with no warning at all
    # until this entry (#824 audit of #806's deprecation-exit criterion).
    "AI_CACHE_PRIMING": "cache-priming serialization removed as dead code in #807",
}
_NOOP_REMOVAL_RELEASE = "v3.0.0"

# Analyzer names removed from ai_pr_review.analyzers.bridge.ANALYZER_NAMES
# (so they never dispatch) but still accepted, as a documented no-op, in the
# analyzers/exclude-analyzers allowlist/denylist inputs (and policy.yml's
# per-route equivalent) -- the same shape as _DEPRECATED_NOOP_AI_VARS above,
# just keyed by analyzer name instead of env-var name. A name here would
# otherwise turn ReviewConfig.from_env()/policy.py's validation into a hard
# ConfigError for any consumer who already references it, which Epic 9's
# no-breaking-changes acceptance criterion (#806) forbids. Formal removal
# (validation then rejects the name outright) is planned for v3.0.0, the
# same release _NOOP_REMOVAL_RELEASE above targets for AI_PROFILE_MAX_TOKENS.
_DEPRECATED_ANALYZER_NAMES: dict[str, str] = {
    # #815: duplicated docs-api-check/docs-ref-check/docs-drift-check's
    # documentation-quality coverage closely enough, at Low severity only
    # (never blocks a merge), to not be worth its maintenance surface (a
    # separate ruff rule-set, a dedicated golangci-lint/godoclint invocation
    # for Go, and a second tree-sitter presence check). Removed, and never
    # dispatches regardless of allow/deny-list membership.
    "docs-missing-check": "removed in #815, docs-api-check/docs-ref-check/docs-drift-check remain",
}

# Env vars whose underlying feature was removed, same shape and intent as
# _DEPRECATED_NOOP_AI_VARS above, but kept in a separate registry because
# _check_unknown_ai_vars only scans keys starting with "AI_" -- none of
# these names ever did, so they need their own unconditional presence check
# (_check_deprecated_noop_env_vars) rather than that scan.
_DEPRECATED_NOOP_ENV_VARS: dict[str, str] = {
    # ReviewConfig.standalone_depth (default 50) was parsed from this var into
    # an int field nothing outside config.py ever read -- reserved for a
    # standalone review mode that was documented but never implemented (see
    # #623). The field itself was removed in #824.
    "STANDALONE_DEPTH": "reserved for standalone review mode, never implemented (#623)",
}


def _check_deprecated_noop_env_vars() -> None:
    """Warn (not raise) for any set-but-inert non-'AI_'-prefixed env var."""
    for key, reason in _DEPRECATED_NOOP_ENV_VARS.items():
        if key in os.environ:
            print(
                f"WARNING: {key!r} is deprecated and ignored ({reason}); "
                f"will be rejected starting in {_NOOP_REMOVAL_RELEASE}.",
                file=sys.stderr,
            )


def _validate_analyzer_names_list(values: tuple[str, ...]) -> tuple[str, ...]:
    """Validate analyzer names, tolerating deprecated-but-inert names.

    Shared by ReviewConfig's analyzers/exclude_analyzers field validator and
    policy.py's per-route analyzer selection (#815), so both surfaces treat a
    deprecated analyzer name the same way: accepted with a warning, never a
    hard error, and never actually dispatched (it is simply absent from
    ANALYZER_NAMES / the analyzer registry).
    """
    from ai_pr_review.analyzers.bridge import ANALYZER_NAMES  # noqa: PLC0415

    for name in values:
        if name in _DEPRECATED_ANALYZER_NAMES:
            print(
                f"WARNING: analyzer {name!r} is deprecated and ignored: "
                f"{_DEPRECATED_ANALYZER_NAMES[name]}.",
                file=sys.stderr,
            )
    return _validate_names_tuple(
        values, ANALYZER_NAMES | frozenset(_DEPRECATED_ANALYZER_NAMES), "analyzer"
    )


def _check_unknown_ai_vars() -> None:
    """Warn (not raise) for any AI_* env var not in the documented set.

    Emitting a warning rather than a ConfigError lets consumers that pin older
    container images continue to work when the action forwards a variable that
    was introduced after the image was built.  The warning still catches typos
    without hard-breaking forward-compatibility.
    """
    for key in os.environ:
        if not key.startswith("AI_"):
            continue
        if key in _KNOWN_AI_VARS:
            continue
        if key in _DEPRECATED_NOOP_AI_VARS:
            reason = _DEPRECATED_NOOP_AI_VARS[key]
            print(
                f"WARNING: {key!r} is deprecated and ignored ({reason}); "
                f"will be rejected starting in {_NOOP_REMOVAL_RELEASE}.",
                file=sys.stderr,
            )
            continue
        if key in _DEPRECATED_AI_VAR_ALIASES:
            canonical = _DEPRECATED_AI_VAR_ALIASES[key]
            if canonical:
                print(
                    f"WARNING: {key!r} is not read by the engine; "
                    f"use {canonical!r} instead.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"WARNING: {key!r} is deprecated and ignored (removed in v2.0.0).",
                    file=sys.stderr,
                )
            continue
        # Find closest documented match for a helpful hint.
        matches = difflib.get_close_matches(key, _KNOWN_AI_VARS, n=1, cutoff=0.6)
        suggestion = f" Did you mean {matches[0]!r}?" if matches else ""
        print(
            f"WARNING: Unknown AI_* variable {key!r} will be ignored.{suggestion}",
            file=sys.stderr,
        )


def _check_deprecated_review_target(value: str) -> None:
    """Warn when REVIEW_TARGET=standalone is set.

    Standalone mode was documented as posting findings to a GitHub/GitLab
    Issue, but that behavior was never ported from the bash engine (removed
    in v2.0.0) to the Python engine. The only remaining effect is disabling
    merge-commit filtering in diff/compute.py. See #623.
    """
    if value == "standalone":
        print(
            "WARNING: REVIEW_TARGET=standalone no longer posts findings to an "
            "issue (see https://github.com/tag1consulting/ai-pr-review/issues/623); "
            "this value now only disables merge-commit filtering during diff "
            "computation. Formal removal is planned for a future major version.",
            file=sys.stderr,
        )


def _validate_names_tuple(
    values: tuple[str, ...],
    valid_names: frozenset[str],
    kind: str,
) -> tuple[str, ...]:
    """Validate that every name in *values* is in *valid_names*.

    Raises ValueError with a nearest-match suggestion for the first unknown name,
    mirroring the behavior of _check_unknown_ai_vars.
    """
    for name in values:
        if name not in valid_names:
            matches = difflib.get_close_matches(name, valid_names, n=1, cutoff=0.6)
            suggestion = f" Did you mean {matches[0]!r}?" if matches else ""
            raise ValueError(f"Unknown {kind} name {name!r}.{suggestion}")
    return values


class ReviewConfig(BaseModel):
    """Typed configuration loaded from environment variables."""

    # --- Core ---
    provider: str = "anthropic"
    model_standard: str = ""
    model_premium: str = ""
    review_mode: str = "quick"
    temperature: float = 0.3
    dry_run: bool = False

    # --- PR / VCS ---
    pr_number: str = ""
    base_ref: str = ""
    head_ref: str = ""
    head_sha: str = ""
    vcs_provider: str = "github"
    review_target: str = "pr"
    force_full_diff: bool = False

    # --- Agent tuning ---
    parallel: bool = True
    # Number of concurrent LLM calls. Derived from parallel in resolve_models().
    concurrency: int = 4
    # Number of concurrent analyzer subprocesses. Clamped to 1 when parallel=False.
    analyzer_concurrency: int = 4
    # Default mirrors action.yml's max-inline default ('25'). Previously 10,
    # which meant CLI users (no action.yml input) saw a different cap than
    # workflow-driven users.
    max_inline: int = 25
    max_tokens_per_agent: int = 32768
    enable_suggestions: bool = True
    llm_prompt_caching: str = "auto"
    confidence_threshold: int = 75
    max_diff_lines: int = 5000

    # --- Agent gates ---
    disable_gate_architecture: bool = False
    disable_gate_security: bool = False
    disable_gate_edge_case: bool = False
    ignore_merge_commits: bool = True

    # --- Context enrichment ---
    enable_context_enrichment: bool = True
    context_max_tokens: int = 8192
    context_lookup_lines: int = 8
    context_max_queries: int = 200

    # --- SARIF ingestion ---
    sarif_paths: tuple[str, ...] = ()

    # --- Diff exclude patterns ---
    exclude_patterns: tuple[str, ...] = ()
    exclude_patterns_mode: str = "append"

    # --- Analyzer and agent allow/deny selection ---
    # Empty tuple (the default) means "no filtering" — all eligible items run.
    # If the allowlist is non-empty, only listed names run (denylist is ignored).
    # If the allowlist is empty, all names except those in the denylist run.
    analyzers: tuple[str, ...] = ()
    exclude_analyzers: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    exclude_agents: tuple[str, ...] = ()

    # --- Analyzer diff-scope ---
    # Controls how out-of-diff native-analyzer findings are handled.
    # "cap"  -- downgrade to Low and collapse into a <details> section (default).
    # "drop" -- remove out-of-diff analyzer findings entirely.
    # "off"  -- pass through unchanged (full-file linting behaviour).
    analyzer_diff_scope: str = "cap"

    # --- Judge pass ---
    # On by default per explicit decision (session 2026-06-22). Adds one cheap-model
    # LLM call per review. Set AI_JUDGE_PASS=false to disable.
    enable_judge_pass: bool = True

    # --- Fail-on-findings ---
    # When true, exit code 2 is returned if the review outcome is REQUEST_CHANGES
    # or COMMENT (incomplete/unknown risk). Designed for CI gates (e.g. Renovate
    # auto-merge blocked until the bot approves). Off by default.
    fail_on_findings: bool = False

    # --- Token usage display (#758) ---
    # How the token-usage/cost information is shown in the posted review
    # comment. The full per-agent breakdown always goes to GITHUB_STEP_SUMMARY
    # (GitHub only) and is always echoed to the CI job log on every provider
    # -- this only controls what appears in the comment itself:
    #   "compact" -- a single cost/token/agent-count summary line (default).
    #   "full"    -- the full <details> table, as posted before this change.
    #   "off"     -- no token-usage content in the comment at all.
    token_usage_display: str = "compact"
    # Absolute estimated-cost threshold (USD) above which a high-usage
    # warning line is added to the comment, separately from whichever
    # token_usage_display payload is shown. 0 disables the warning entirely.
    token_usage_warn_usd: float = 1.00

    # --- Slash commands + feedback loop ---
    enable_feedback_loop: bool = False
    feedback_branch: str = "ai-pr-review-bot"
    feedback_max_tokens: int = 2048
    feedback_retention_count: int = 500
    feedback_retention_age_days: int = 365

    # --- Provider credentials ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    bedrock_api_key: str = ""
    bedrock_api_url: str = ""
    openai_base_url: str = ""
    gh_token: str = ""

    # --- GitHub ---
    github_repository: str = ""

    # --- Bitbucket ---
    bitbucket_email: str = ""
    bitbucket_api_token: str = ""
    bitbucket_workspace: str = ""
    bitbucket_repo_slug: str = ""

    # --- GitLab ---
    gitlab_token: str = ""
    gitlab_api_url: str = "https://gitlab.com"
    gitlab_project_id: str = ""
    gitlab_mr_diff_base_sha: str = ""
    gitlab_bot_username: str = ""
    ci_project_id: str = ""
    ci_project_path: str = ""
    ci_merge_request_iid: str = ""
    ci_merge_request_diff_base_sha: str = ""
    ci_job_token: str = ""

    # --- Recording ---
    record_dir: str = ""

    # --- Structured logging ---
    log_format: str = "human"
    log_level: str = "WARNING"

    # --- Telemetry ---
    telemetry_enabled: bool = False
    telemetry_sink: str = ""

    @field_validator("review_mode")
    @classmethod
    def _validate_review_mode(cls, v: str) -> str:
        # '' means "not explicitly set" (action.yml's review-mode input
        # defaults to '' — see #policy.py) — deferred to policy.yml routing,
        # else the hardcoded 'quick' default, resolved in
        # review/runtime.py:build_review_runtime before dispatch. It is
        # never a valid mode by the time agents actually run.
        if v not in ("", "quick", "full"):
            raise ValueError(f"review_mode must be 'quick' or 'full', got {v!r}")
        return v

    @field_validator("vcs_provider")
    @classmethod
    def _validate_vcs_provider(cls, v: str) -> str:
        if v not in ("github", "bitbucket", "gitlab"):
            raise ValueError(f"vcs_provider must be github/bitbucket/gitlab, got {v!r}")
        return v

    @field_validator("exclude_patterns_mode")
    @classmethod
    def _validate_exclude_patterns_mode(cls, v: str) -> str:
        normalized = v.lower()
        if normalized not in ("append", "replace"):
            raise ValueError(
                f"exclude_patterns_mode must be 'append' or 'replace', got {v!r}"
            )
        return normalized

    @field_validator("analyzers", "exclude_analyzers")
    @classmethod
    def _validate_analyzer_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            return v
        # Lazy import (inside _validate_analyzer_names_list) to avoid pulling
        # all native analyzer modules into config at startup.
        return _validate_analyzer_names_list(v)

    @field_validator("agents", "exclude_agents")
    @classmethod
    def _validate_agent_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            return v
        from ai_pr_review.agents.roster import AGENT_NAMES  # noqa: PLC0415
        return _validate_names_tuple(v, AGENT_NAMES, "agent")

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, v: float) -> float:
        if not (0.0 <= v <= 2.0):
            raise ValueError(f"temperature must be in [0, 2], got {v}")
        return v

    @field_validator("confidence_threshold")
    @classmethod
    def _validate_confidence(cls, v: int) -> int:
        if not (0 <= v <= 100):
            raise ValueError(f"confidence_threshold must be 0-100, got {v}")
        return v

    @field_validator("max_tokens_per_agent")
    @classmethod
    def _clamp_max_tokens_per_agent(cls, v: int) -> int:
        _MIN, _MAX = 256, 65536
        if v < _MIN:
            print(
                f"WARNING: AI_MAX_TOKENS_PER_AGENT={v} is below minimum {_MIN}; clamping to {_MIN}. "
                "Review will proceed with this value.",
                file=sys.stderr,
            )
            return _MIN
        if v > _MAX:
            print(
                f"WARNING: AI_MAX_TOKENS_PER_AGENT={v} exceeds maximum {_MAX}; clamping to {_MAX}. "
                "Review will proceed with this value.",
                file=sys.stderr,
            )
            return _MAX
        return v

    @field_validator("analyzer_diff_scope")
    @classmethod
    def _validate_analyzer_diff_scope(cls, v: str) -> str:
        if v not in ("cap", "drop", "off"):
            raise ValueError(f"analyzer_diff_scope must be 'cap', 'drop', or 'off', got {v!r}")
        return v

    @field_validator("token_usage_display")
    @classmethod
    def _validate_token_usage_display(cls, v: str) -> str:
        normalized = v.lower()
        if normalized not in ("compact", "full", "off"):
            raise ValueError(
                f"token_usage_display must be 'compact', 'full', or 'off', got {v!r}"
            )
        return normalized

    @field_validator("token_usage_warn_usd")
    @classmethod
    def _clamp_token_usage_warn_usd(cls, v: float) -> float:
        if v < 0:
            print(
                f"WARNING: AI_TOKEN_USAGE_WARN_USD={v} is negative; clamping to 0 "
                "(warning disabled). Review will proceed with this value.",
                file=sys.stderr,
            )
            return 0.0
        return v

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        if v not in ("human", "json"):
            raise ValueError(f"log_format must be 'human' or 'json', got {v!r}")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}, got {v!r}")
        return v.upper()

    @model_validator(mode="before")
    @classmethod
    def _from_env(cls, data: object) -> object:
        """When called with no args, load from environment."""
        if data:
            return data
        return {}

    @classmethod
    def from_env(cls) -> ReviewConfig:
        """Load config from environment variables. Raises ConfigError on unknown AI_* vars."""
        _check_unknown_ai_vars()
        _check_deprecated_noop_env_vars()

        review_target = os.environ.get("REVIEW_TARGET", "pr").strip().lower()
        _check_deprecated_review_target(review_target)

        def _bool(key: str, default: bool = False) -> bool:
            return os.environ.get(key, "true" if default else "false").lower() in (
                "true",
                "1",
                "yes",
            )

        def _int(key: str, default: int) -> int:
            raw = os.environ.get(key, str(default))
            try:
                return int(raw)
            except ValueError:
                print(
                    f"WARNING: {key}={raw!r} is not a valid integer; using default {default}. Review will proceed with this default.",
                    file=sys.stderr,
                )
                return default

        def _float(key: str, default: float) -> float:
            raw = os.environ.get(key, str(default))
            try:
                return float(raw)
            except ValueError:
                print(
                    f"WARNING: {key}={raw!r} is not a valid float; using default {default}. Review will proceed with this default.",
                    file=sys.stderr,
                )
                return default

        return cls(
            provider=os.environ.get("AI_PROVIDER", "anthropic").strip(),
            model_standard=os.environ.get("AI_MODEL_STANDARD", "").strip(),
            model_premium=os.environ.get("AI_MODEL_PREMIUM", "").strip(),
            review_mode=os.environ.get("AI_REVIEW_MODE", "quick").strip(),
            temperature=_float("AI_TEMPERATURE", 0.3),
            dry_run=_bool("AI_DRY_RUN"),
            pr_number=os.environ.get("PR_NUMBER", ""),
            base_ref=os.environ.get("BASE_REF", ""),
            head_ref=os.environ.get("HEAD_REF", ""),
            head_sha=os.environ.get("HEAD_SHA", ""),
            vcs_provider=os.environ.get("VCS_PROVIDER", "github").strip(),
            review_target=review_target,
            force_full_diff=_bool("FORCE_FULL_DIFF"),
            parallel=_bool("AI_PARALLEL", True),
            analyzer_concurrency=max(1, _int("AI_ANALYZER_CONCURRENCY", 4)),
            max_inline=_int("AI_MAX_INLINE", 25),
            max_tokens_per_agent=_int("AI_MAX_TOKENS_PER_AGENT", 32768),
            enable_suggestions=_bool("AI_ENABLE_SUGGESTIONS", True),
            llm_prompt_caching=os.environ.get("LLM_PROMPT_CACHING", "auto"),
            confidence_threshold=_int("AI_CONFIDENCE_THRESHOLD", 75),
            max_diff_lines=_int("MAX_DIFF_LINES", 5000),
            disable_gate_architecture=_bool("AI_DISABLE_GATE_ARCHITECTURE"),
            disable_gate_security=_bool("AI_DISABLE_GATE_SECURITY"),
            disable_gate_edge_case=_bool("AI_DISABLE_GATE_EDGE_CASE"),
            ignore_merge_commits=_bool("AI_IGNORE_MERGE_COMMITS", True),
            enable_context_enrichment=_bool("AI_CONTEXT_ENRICHMENT", True),
            context_max_tokens=_int("AI_CONTEXT_MAX_TOKENS", 8192),
            context_lookup_lines=_int("AI_CONTEXT_LOOKUP_LINES", 8),
            context_max_queries=_int("AI_CONTEXT_MAX_QUERIES", 200),
            sarif_paths=tuple(
                p.strip()
                for p in os.environ.get("AI_SARIF_PATHS", "").split(",")
                if p.strip()
            ),
            exclude_patterns=tuple(
                p.strip()
                for p in os.environ.get("AI_EXCLUDE_PATTERNS", "").split(",")
                if p.strip()
            ),
            exclude_patterns_mode=os.environ.get("AI_EXCLUDE_PATTERNS_MODE", "append"),
            analyzers=tuple(
                p.strip()
                for p in os.environ.get("AI_ANALYZERS", "").split(",")
                if p.strip()
            ),
            exclude_analyzers=tuple(
                p.strip()
                for p in os.environ.get("AI_EXCLUDE_ANALYZERS", "").split(",")
                if p.strip()
            ),
            agents=tuple(
                p.strip()
                for p in os.environ.get("AI_AGENTS", "").split(",")
                if p.strip()
            ),
            exclude_agents=tuple(
                p.strip()
                for p in os.environ.get("AI_EXCLUDE_AGENTS", "").split(",")
                if p.strip()
            ),
            analyzer_diff_scope=os.environ.get("AI_ANALYZER_DIFF_SCOPE", "cap"),
            enable_judge_pass=_bool("AI_JUDGE_PASS", True),
            fail_on_findings=_bool("AI_FAIL_ON_FINDINGS"),
            token_usage_display=os.environ.get("AI_TOKEN_USAGE_DISPLAY", "compact").strip() or "compact",
            token_usage_warn_usd=_float("AI_TOKEN_USAGE_WARN_USD", 1.00),
            enable_feedback_loop=_bool("AI_FEEDBACK_LOOP"),
            feedback_branch=os.environ.get("AI_FEEDBACK_BRANCH", "ai-pr-review-bot"),
            feedback_max_tokens=_int("AI_FEEDBACK_MAX_TOKENS", 2048),
            feedback_retention_count=_int("AI_FEEDBACK_RETENTION_COUNT", 500),
            feedback_retention_age_days=_int("AI_FEEDBACK_RETENTION_AGE_DAYS", 365),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            google_api_key=os.environ.get("GOOGLE_API_KEY", "").strip(),
            bedrock_api_key=os.environ.get("BEDROCK_API_KEY", "").strip(),
            bedrock_api_url=os.environ.get("BEDROCK_API_URL", "").strip(),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "").strip(),
            gh_token=(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip(),
            github_repository=os.environ.get("GITHUB_REPOSITORY", "").strip(),
            bitbucket_email=os.environ.get("BITBUCKET_EMAIL", ""),
            bitbucket_api_token=os.environ.get("BITBUCKET_API_TOKEN", "").strip(),
            bitbucket_workspace=os.environ.get("BITBUCKET_WORKSPACE", ""),
            bitbucket_repo_slug=os.environ.get("BITBUCKET_REPO_SLUG", ""),
            gitlab_token=os.environ.get("GITLAB_TOKEN", "").strip(),
            gitlab_api_url=os.environ.get("GITLAB_API_URL", "https://gitlab.com").strip(),
            gitlab_project_id=os.environ.get("GITLAB_PROJECT_ID", ""),
            gitlab_mr_diff_base_sha=os.environ.get("GITLAB_MR_DIFF_BASE_SHA", ""),
            gitlab_bot_username=os.environ.get("GITLAB_BOT_USERNAME", ""),
            ci_project_id=os.environ.get("CI_PROJECT_ID", ""),
            ci_project_path=os.environ.get("CI_PROJECT_PATH", ""),
            ci_merge_request_iid=os.environ.get("CI_MERGE_REQUEST_IID", ""),
            ci_merge_request_diff_base_sha=os.environ.get(
                "CI_MERGE_REQUEST_DIFF_BASE_SHA", ""
            ),
            ci_job_token=os.environ.get("CI_JOB_TOKEN", "").strip(),
            record_dir=os.environ.get("AI_PR_REVIEW_RECORD_DIR", ""),
            log_format=os.environ.get("AI_LOG_FORMAT", "human"),
            log_level=os.environ.get("AI_LOG_LEVEL", "WARNING"),
            telemetry_enabled=_bool("AI_TELEMETRY_ENABLED", False),
            telemetry_sink=os.environ.get("AI_TELEMETRY_SINK", ""),
        )

    def resolve_models(self) -> ReviewConfig:
        """Return a copy with provider model defaults applied.

        Fills in AI_MODEL_STANDARD / AI_MODEL_PREMIUM defaults per provider
        when they are not set in the environment.
        openai-compatible is left as-is (user must specify).
        """
        _PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
            "anthropic":    ("claude-sonnet-5",                     "claude-opus-5"),
            "openai":       ("gpt-5.4-mini",                        "gpt-5.4"),
            "google":       ("gemini-2.5-flash",                    "gemini-2.5-pro"),
            "bedrock-proxy": ("us.anthropic.claude-sonnet-5",       "global.anthropic.claude-opus-4-7"),
        }
        std = self.model_standard
        prem = self.model_premium
        if self.provider in _PROVIDER_DEFAULTS:
            default_std, default_prem = _PROVIDER_DEFAULTS[self.provider]
            std = std or default_std
            prem = prem or default_prem
        elif self.provider == "openai-compatible":
            # No universal default; keep as-is and let DispatchContext validate.
            prem = prem or std
        else:
            if not std or not prem:
                valid = list(_PROVIDER_DEFAULTS) + ["openai-compatible"]
                raise ConfigError(
                    f"Unknown provider {self.provider!r}; no model defaults available. "
                    f"Set AI_MODEL_STANDARD and AI_MODEL_PREMIUM. Valid built-in providers: {valid}."
                )

        # AI_PARALLEL=true → 4 concurrent calls (bash default); false → 1 (serial).
        concurrency = 4 if self.parallel else 1
        # Mirror: parallel=false also serializes analyzer subprocesses.
        analyzer_concurrency = 1 if not self.parallel else self.analyzer_concurrency

        return self.model_copy(update={
            "model_standard": std,
            "model_premium": prem,
            "concurrency": concurrency,
            "analyzer_concurrency": analyzer_concurrency,
        })
