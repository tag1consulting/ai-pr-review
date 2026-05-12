"""Typed configuration for the AI PR Review engine.

Maps every environment variable from the Epic 0 config matrix into a
pydantic ReviewConfig. Unknown AI_* vars raise ConfigError with a
nearest-match suggestion.
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
        "AI_PR_REVIEW_RECORD_DIR",
        "AI_PR_REVIEW_ENGINE",
        "AI_PR_REVIEW_COMPUTE_OUTPUT",
        # Claude Code sets this in its agent environment; not a user-configured var.
        "AI_AGENT",
    }
)


def _check_unknown_ai_vars() -> None:
    """Raise ConfigError for any AI_* env var not in the documented set."""
    for key in os.environ:
        if not key.startswith("AI_"):
            continue
        if key in _KNOWN_AI_VARS:
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
    max_inline: int = 10
    max_tokens_per_agent: int = 4096
    enable_suggestions: bool = True
    cache_priming: bool = True
    llm_prompt_caching: str = "auto"
    confidence_threshold: int = 75
    max_diff_lines: int = 5000
    llm_retry_count: int = 2

    # --- Agent gates ---
    disable_gate_architecture: bool = False
    disable_gate_security: bool = False
    disable_gate_edge_case: bool = False

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
    phpstan_level: int = 5

    # --- Engine / recording ---
    engine: str = "bash"
    record_dir: str = ""
    compute_output: str = ""

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

    @field_validator("confidence_threshold")
    @classmethod
    def _validate_confidence(cls, v: int) -> int:
        if not (0 <= v <= 100):
            raise ValueError(f"confidence_threshold must be 0-100, got {v}")
        return v

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
            try:
                return float(os.environ.get(key, str(default)))
            except ValueError:
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
            max_inline=_int("AI_MAX_INLINE", 10),
            max_tokens_per_agent=_int("AI_MAX_TOKENS_PER_AGENT", 4096),
            enable_suggestions=_bool("AI_ENABLE_SUGGESTIONS", True),
            cache_priming=_bool("AI_CACHE_PRIMING", True),
            llm_prompt_caching=os.environ.get("LLM_PROMPT_CACHING", "auto"),
            confidence_threshold=_int("AI_CONFIDENCE_THRESHOLD", 75),
            max_diff_lines=_int("MAX_DIFF_LINES", 5000),
            llm_retry_count=_int("LLM_RETRY_COUNT", 2),
            disable_gate_architecture=_bool("AI_DISABLE_GATE_ARCHITECTURE"),
            disable_gate_security=_bool("AI_DISABLE_GATE_SECURITY"),
            disable_gate_edge_case=_bool("AI_DISABLE_GATE_EDGE_CASE"),
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
            phpstan_level=_int("PHPSTAN_LEVEL", 5),
            engine=os.environ.get("AI_PR_REVIEW_ENGINE", "bash"),
            record_dir=os.environ.get("AI_PR_REVIEW_RECORD_DIR", ""),
            compute_output=os.environ.get("AI_PR_REVIEW_COMPUTE_OUTPUT", ""),
        )
