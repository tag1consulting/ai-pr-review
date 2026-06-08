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
        "AI_CACHE_PRIMING",
        "AI_CONFIDENCE_THRESHOLD",
        "AI_DISABLE_GATE_ARCHITECTURE",
        "AI_DISABLE_GATE_SECURITY",
        "AI_DISABLE_GATE_EDGE_CASE",
        "AI_DRY_RUN",
        "AI_IGNORE_MERGE_COMMITS",
        "AI_PR_REVIEW_RECORD_DIR",
        "AI_PR_REVIEW_ENGINE",
        "AI_PR_REVIEW_COMPUTE_OUTPUT",
        "AI_PR_REVIEW_SCRIPT_DIR",
        # Claude Code sets this in its agent environment; not a user-configured var.
        "AI_AGENT",
        # --- Context enrichment ---
        "AI_CONTEXT_ENRICHMENT",
        "AI_CONTEXT_MAX_TOKENS",
        "AI_CONTEXT_LOOKUP_LINES",
        # --- SARIF ingestion ---
        "AI_SARIF_PATHS",
        # --- Diff exclude patterns ---
        "AI_EXCLUDE_PATTERNS",
        "AI_EXCLUDE_PATTERNS_MODE",
        # --- Analyzer diff scope ---
        "AI_ANALYZER_DIFF_SCOPE",
        # --- Slash commands + feedback loop ---
        "AI_FEEDBACK_LOOP",
        "AI_FEEDBACK_BRANCH",
        "AI_FEEDBACK_MAX_TOKENS",
        "AI_FEEDBACK_RETENTION_COUNT",
        "AI_FEEDBACK_RETENTION_AGE_DAYS",
        # --- Structured logging ---
        "AI_LOG_FORMAT",
        "AI_LOG_LEVEL",
        # Set by the engine at startup and inherited by analyzer subprocesses;
        # not a user-configured input but must be known to avoid ConfigError.
        "AI_PR_REVIEW_CORRELATION_ID",
        # --- Analyzer concurrency ---
        "AI_ANALYZER_CONCURRENCY",
        # --- Telemetry ---
        "AI_TELEMETRY_ENABLED",
        "AI_TELEMETRY_SINK",
    }
)


# Variables that are accepted without error but have no effect on the engine.
# A deprecation warning is printed so users know to use the canonical name.
_DEPRECATED_AI_VAR_ALIASES: dict[str, str] = {
    # GitHub Actions workflow uses AI_REVIEW_IGNORE_MERGE_COMMITS as a repo
    # variable name; the engine reads AI_IGNORE_MERGE_COMMITS.
    "AI_REVIEW_IGNORE_MERGE_COMMITS": "AI_IGNORE_MERGE_COMMITS",
}


def _check_unknown_ai_vars() -> None:
    """Raise ConfigError for any AI_* env var not in the documented set."""
    for key in os.environ:
        if not key.startswith("AI_"):
            continue
        if key in _KNOWN_AI_VARS:
            continue
        if key in _DEPRECATED_AI_VAR_ALIASES:
            canonical = _DEPRECATED_AI_VAR_ALIASES[key]
            print(
                f"WARNING: {key!r} is not read by the engine; "
                f"use {canonical!r} instead.",
                file=sys.stderr,
            )
            continue
        # Find closest documented match for a helpful error.
        matches = difflib.get_close_matches(key, _KNOWN_AI_VARS, n=1, cutoff=0.6)
        suggestion = f" Did you mean {matches[0]!r}?" if matches else ""
        raise ConfigError(f"Unknown AI_* variable {key!r}.{suggestion}")


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
    head_sha: str = ""
    vcs_provider: str = "github"
    review_target: str = "pr"
    force_full_diff: bool = False
    standalone_depth: int = 50

    # --- Agent tuning ---
    parallel: bool = True
    # Number of concurrent LLM calls. Derived from parallel in resolve_models().
    concurrency: int = 4
    # Number of concurrent analyzer subprocesses. Clamped to 1 when parallel=False.
    analyzer_concurrency: int = 4
    max_inline: int = 10
    max_tokens_per_agent: int = 16384
    enable_suggestions: bool = True
    cache_priming: bool = False
    llm_prompt_caching: str = "auto"
    confidence_threshold: int = 75
    max_diff_lines: int = 5000
    llm_retry_count: int = 2

    # --- Agent gates ---
    disable_gate_architecture: bool = False
    disable_gate_security: bool = False
    disable_gate_edge_case: bool = False
    ignore_merge_commits: bool = True

    # --- Context enrichment ---
    enable_context_enrichment: bool = True
    context_max_tokens: int = 8192
    context_lookup_lines: int = 8

    # --- SARIF ingestion ---
    sarif_paths: tuple[str, ...] = ()

    # --- Diff exclude patterns ---
    exclude_patterns: tuple[str, ...] = ()
    exclude_patterns_mode: str = "append"

    # --- Analyzer diff-scope ---
    # Controls how out-of-diff native-analyzer findings are handled.
    # "cap"  -- downgrade to Low and collapse into a <details> section (default).
    # "drop" -- remove out-of-diff analyzer findings entirely.
    # "off"  -- pass through unchanged (full-file linting behaviour).
    analyzer_diff_scope: str = "cap"

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

    # --- PHP ---
    phpstan_level: int = 3

    # --- Engine / recording ---
    engine: str = "python"
    record_dir: str = ""
    compute_output: str = ""

    # --- Structured logging ---
    log_format: str = "human"
    log_level: str = "WARNING"

    # --- Telemetry ---
    telemetry_enabled: bool = False
    telemetry_sink: str = ""

    @field_validator("review_mode")
    @classmethod
    def _validate_review_mode(cls, v: str) -> str:
        if v not in ("quick", "full"):
            raise ValueError(f"review_mode must be 'quick' or 'full', got {v!r}")
        return v

    @field_validator("vcs_provider")
    @classmethod
    def _validate_vcs_provider(cls, v: str) -> str:
        if v not in ("github", "bitbucket", "gitlab"):
            raise ValueError(f"vcs_provider must be github/bitbucket/gitlab, got {v!r}")
        return v

    @field_validator("engine")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        if v not in ("bash", "python"):
            raise ValueError(f"engine must be 'bash' or 'python', got {v!r}")
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
            provider=os.environ.get("AI_PROVIDER", "anthropic"),
            model_standard=os.environ.get("AI_MODEL_STANDARD", ""),
            model_premium=os.environ.get("AI_MODEL_PREMIUM", ""),
            review_mode=os.environ.get("AI_REVIEW_MODE", "quick"),
            temperature=_float("AI_TEMPERATURE", 0.3),
            dry_run=_bool("AI_DRY_RUN"),
            pr_number=os.environ.get("PR_NUMBER", ""),
            base_ref=os.environ.get("BASE_REF", ""),
            head_sha=os.environ.get("HEAD_SHA", ""),
            vcs_provider=os.environ.get("VCS_PROVIDER", "github"),
            review_target=os.environ.get("REVIEW_TARGET", "pr"),
            force_full_diff=_bool("FORCE_FULL_DIFF"),
            standalone_depth=_int("STANDALONE_DEPTH", 50),
            parallel=_bool("AI_PARALLEL", True),
            analyzer_concurrency=max(1, _int("AI_ANALYZER_CONCURRENCY", 4)),
            max_inline=_int("AI_MAX_INLINE", 10),
            max_tokens_per_agent=_int("AI_MAX_TOKENS_PER_AGENT", 16384),
            enable_suggestions=_bool("AI_ENABLE_SUGGESTIONS", True),
            cache_priming=_bool("AI_CACHE_PRIMING", False),
            llm_prompt_caching=os.environ.get("LLM_PROMPT_CACHING", "auto"),
            confidence_threshold=_int("AI_CONFIDENCE_THRESHOLD", 75),
            max_diff_lines=_int("MAX_DIFF_LINES", 5000),
            llm_retry_count=_int("LLM_RETRY_COUNT", 2),
            disable_gate_architecture=_bool("AI_DISABLE_GATE_ARCHITECTURE"),
            disable_gate_security=_bool("AI_DISABLE_GATE_SECURITY"),
            disable_gate_edge_case=_bool("AI_DISABLE_GATE_EDGE_CASE"),
            ignore_merge_commits=_bool("AI_IGNORE_MERGE_COMMITS", True),
            enable_context_enrichment=_bool("AI_CONTEXT_ENRICHMENT", True),
            context_max_tokens=_int("AI_CONTEXT_MAX_TOKENS", 8192),
            context_lookup_lines=_int("AI_CONTEXT_LOOKUP_LINES", 8),
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
            analyzer_diff_scope=os.environ.get("AI_ANALYZER_DIFF_SCOPE", "cap"),
            enable_feedback_loop=_bool("AI_FEEDBACK_LOOP"),
            feedback_branch=os.environ.get("AI_FEEDBACK_BRANCH", "ai-pr-review-bot"),
            feedback_max_tokens=_int("AI_FEEDBACK_MAX_TOKENS", 2048),
            feedback_retention_count=_int("AI_FEEDBACK_RETENTION_COUNT", 500),
            feedback_retention_age_days=_int("AI_FEEDBACK_RETENTION_AGE_DAYS", 365),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            google_api_key=os.environ.get("GOOGLE_API_KEY", ""),
            bedrock_api_key=os.environ.get("BEDROCK_API_KEY", ""),
            bedrock_api_url=os.environ.get("BEDROCK_API_URL", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", ""),
            gh_token=os.environ.get("GH_TOKEN", ""),
            github_repository=os.environ.get("GITHUB_REPOSITORY", ""),
            bitbucket_email=os.environ.get("BITBUCKET_EMAIL", ""),
            bitbucket_api_token=os.environ.get("BITBUCKET_API_TOKEN", ""),
            bitbucket_workspace=os.environ.get("BITBUCKET_WORKSPACE", ""),
            bitbucket_repo_slug=os.environ.get("BITBUCKET_REPO_SLUG", ""),
            gitlab_token=os.environ.get("GITLAB_TOKEN", ""),
            gitlab_api_url=os.environ.get("GITLAB_API_URL", "https://gitlab.com"),
            gitlab_project_id=os.environ.get("GITLAB_PROJECT_ID", ""),
            gitlab_mr_diff_base_sha=os.environ.get("GITLAB_MR_DIFF_BASE_SHA", ""),
            gitlab_bot_username=os.environ.get("GITLAB_BOT_USERNAME", ""),
            ci_project_id=os.environ.get("CI_PROJECT_ID", ""),
            ci_project_path=os.environ.get("CI_PROJECT_PATH", ""),
            ci_merge_request_iid=os.environ.get("CI_MERGE_REQUEST_IID", ""),
            ci_merge_request_diff_base_sha=os.environ.get(
                "CI_MERGE_REQUEST_DIFF_BASE_SHA", ""
            ),
            ci_job_token=os.environ.get("CI_JOB_TOKEN", ""),
            phpstan_level=_int("PHPSTAN_LEVEL", 3),
            engine=os.environ.get("AI_PR_REVIEW_ENGINE", "python"),
            record_dir=os.environ.get("AI_PR_REVIEW_RECORD_DIR", ""),
            compute_output=os.environ.get("AI_PR_REVIEW_COMPUTE_OUTPUT", ""),
            log_format=os.environ.get("AI_LOG_FORMAT", "human"),
            log_level=os.environ.get("AI_LOG_LEVEL", "WARNING"),
            telemetry_enabled=_bool("AI_TELEMETRY_ENABLED", False),
            telemetry_sink=os.environ.get("AI_TELEMETRY_SINK", ""),
        )

    def resolve_models(self) -> ReviewConfig:
        """Return a copy with provider model defaults applied.

        Mirrors the provider default table in review.sh so direct Python
        invocation works without bash pre-filling AI_MODEL_STANDARD.
        openai-compatible is left as-is (user must specify).
        """
        _PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
            "anthropic":    ("claude-sonnet-4-6",                   "claude-opus-4-8"),
            "openai":       ("gpt-5.4-mini",                        "gpt-5.4"),
            "google":       ("gemini-2.5-flash",                    "gemini-2.5-pro"),
            "bedrock-proxy": ("us.anthropic.claude-sonnet-4-6",     "global.anthropic.claude-opus-4-7"),
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
