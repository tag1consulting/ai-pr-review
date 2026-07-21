"""Tests for ai_pr_review.config (S3 — typed config)."""

import os
import re
from pathlib import Path

import pytest

import ai_pr_review.config as _config_module
from ai_pr_review.config import (
    _DEPRECATED_AI_VAR_ALIASES,
    _KNOWN_AI_VARS,
    ReviewConfig,
)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env() should produce sensible defaults when env is clean."""
    # Strip all relevant env vars
    for key in list(os.environ.keys()):
        if key.startswith("AI_") or key in (
            "PR_NUMBER", "BASE_REF", "HEAD_SHA", "VCS_PROVIDER",
            "REVIEW_TARGET", "GH_TOKEN", "GITHUB_REPOSITORY",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
            "BEDROCK_API_KEY", "BEDROCK_API_URL", "OPENAI_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)

    cfg = ReviewConfig.from_env()
    assert cfg.provider == "anthropic"
    assert cfg.review_mode == "quick"
    assert cfg.confidence_threshold == 75
    assert cfg.max_diff_lines == 5000
    assert cfg.parallel is True
    assert cfg.ignore_merge_commits is True
    # action.yml's max-inline default is '25'; CLI default must match so
    # that workflow-driven and CLI-only callers see the same cap.
    assert cfg.max_inline == 25


def test_max_inline_default_matches_action_yml() -> None:
    """The bare-constructor default for max_inline must match action.yml's
    documented default ('25'), so a caller that imports ReviewConfig
    directly sees the same cap as a caller invoked via the action.
    """
    cfg = ReviewConfig()
    assert cfg.max_inline == 25


def test_ignore_merge_commits_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ignore-merge-commits defaults to True when AI_IGNORE_MERGE_COMMITS is unset."""
    monkeypatch.delenv("AI_IGNORE_MERGE_COMMITS", raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.ignore_merge_commits is True


def test_ignore_merge_commits_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting AI_IGNORE_MERGE_COMMITS=false restores the old behavior."""
    monkeypatch.setenv("AI_IGNORE_MERGE_COMMITS", "false")
    cfg = ReviewConfig.from_env()
    assert cfg.ignore_merge_commits is False


def test_override_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_REVIEW_MODE", "full")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "50")
    cfg = ReviewConfig.from_env()
    assert cfg.review_mode == "full"
    assert cfg.confidence_threshold == 50


def test_secrets_and_variables_stripped_of_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#600: trailing newline/whitespace on secrets and provider/model
    variables must not survive into ReviewConfig, since these values are
    also the source of Layer 3 secret masking (cli._secret_set) — an
    unstripped value there would fail to mask the stripped value actually
    sent over the wire by the LLM/VCS provider modules.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "\tsecret-key\n")
    monkeypatch.setenv("OPENAI_API_KEY", "  secret-key2  ")
    monkeypatch.setenv("GOOGLE_API_KEY", "secret-key3\t")
    monkeypatch.setenv("BEDROCK_API_KEY", " secret-key4")
    monkeypatch.setenv("BEDROCK_API_URL", " https://bedrock.example/ \n")
    monkeypatch.setenv("OPENAI_BASE_URL", "\thttps://openai.example/v1")
    monkeypatch.setenv("GH_TOKEN", "\ngh-token ")
    monkeypatch.setenv("GITLAB_TOKEN", " gl-token\t")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "bb-token\n")
    monkeypatch.setenv("CI_JOB_TOKEN", " ci-token ")
    monkeypatch.setenv("AI_PROVIDER", "  anthropic\n")
    monkeypatch.setenv("AI_MODEL_STANDARD", "\tclaude-sonnet-5 ")
    monkeypatch.setenv("AI_MODEL_PREMIUM", " claude-opus-4-8\n")
    monkeypatch.setenv("AI_REVIEW_MODE", "\tfull\n")
    monkeypatch.setenv("VCS_PROVIDER", " gitlab ")
    monkeypatch.setenv("REVIEW_TARGET", "standalone\t")

    cfg = ReviewConfig.from_env()
    assert cfg.anthropic_api_key == "secret-key"
    assert cfg.openai_api_key == "secret-key2"
    assert cfg.google_api_key == "secret-key3"
    assert cfg.bedrock_api_key == "secret-key4"
    assert cfg.bedrock_api_url == "https://bedrock.example/"
    assert cfg.openai_base_url == "https://openai.example/v1"
    assert cfg.gh_token == "gh-token"
    assert cfg.gitlab_token == "gl-token"
    assert cfg.bitbucket_api_token == "bb-token"
    assert cfg.ci_job_token == "ci-token"
    assert cfg.provider == "anthropic"
    assert cfg.model_standard == "claude-sonnet-5"
    assert cfg.model_premium == "claude-opus-4-8"
    assert cfg.review_mode == "full"
    assert cfg.vcs_provider == "gitlab"
    assert cfg.review_target == "standalone"


def test_gh_token_falls_back_to_github_token_and_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh_token must fall back to GITHUB_TOKEN (the GitHub Actions default env
    var) the same way vcs/__init__.py and feedback/store.py already do, since
    this field is also the source of Layer 3 secret masking (cli._secret_set)
    — without the fallback, a deployment that sets only GITHUB_TOKEN would
    have its live token missing from the redaction set.
    """
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "\tfallback-token\n")

    cfg = ReviewConfig.from_env()
    assert cfg.gh_token == "fallback-token"


def test_unknown_ai_var_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AI_TOTALLY_MADE_UP", "1")
    ReviewConfig.from_env()  # must not raise
    captured = capsys.readouterr()
    assert "AI_TOTALLY_MADE_UP" in captured.err
    assert "WARNING" in captured.err


def test_engine_env_var_deprecated(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """AI_PR_REVIEW_ENGINE is deprecated in v2.0.0; emits a warning but does not raise."""
    monkeypatch.setenv("AI_PR_REVIEW_ENGINE", "python")
    ReviewConfig.from_env()
    captured = capsys.readouterr()
    assert "AI_PR_REVIEW_ENGINE" in captured.err
    assert "deprecated" in captured.err


def test_unknown_ai_var_typo_suggestion(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AI_REVIEW_MDOE", "quick")  # typo: MDOE vs MODE
    ReviewConfig.from_env()  # must not raise
    captured = capsys.readouterr()
    assert "AI_REVIEW_MDOE" in captured.err
    assert "AI_REVIEW_MODE" in captured.err  # typo suggestion still appears


def test_review_target_standalone_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#623: REVIEW_TARGET=standalone no longer posts findings to an issue;
    emits a warning but does not raise, and review_target is still set."""
    monkeypatch.setenv("REVIEW_TARGET", "standalone")
    cfg = ReviewConfig.from_env()
    assert cfg.review_target == "standalone"
    captured = capsys.readouterr()
    assert "REVIEW_TARGET=standalone" in captured.err
    assert "#623" in captured.err or "issues/623" in captured.err


def test_review_target_pr_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_TARGET", "pr")
    ReviewConfig.from_env()
    captured = capsys.readouterr()
    assert "standalone" not in captured.err


def test_deprecated_alias_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AI_REVIEW_IGNORE_MERGE_COMMITS is accepted without ConfigError."""
    monkeypatch.setenv("AI_REVIEW_IGNORE_MERGE_COMMITS", "true")
    ReviewConfig.from_env()  # must not raise
    stderr = capsys.readouterr().err
    assert "AI_REVIEW_IGNORE_MERGE_COMMITS" in stderr
    assert "AI_IGNORE_MERGE_COMMITS" in stderr


def test_deprecated_aliases_are_disjoint_from_known_vars() -> None:
    """Deprecated aliases must NOT be in _KNOWN_AI_VARS.

    If a key appears in both, _check_unknown_ai_vars hits the _KNOWN_AI_VARS
    branch first and skips the deprecation warning entirely.
    """
    overlap = set(_DEPRECATED_AI_VAR_ALIASES) & _KNOWN_AI_VARS
    assert not overlap, (
        f"Keys in both _DEPRECATED_AI_VAR_ALIASES and _KNOWN_AI_VARS "
        f"(warning will never fire): {overlap}"
    )


def test_invalid_review_mode() -> None:
    with pytest.raises(ValueError):
        ReviewConfig.model_validate({"review_mode": "invalid"})


def test_invalid_confidence_threshold() -> None:
    with pytest.raises(ValueError):
        ReviewConfig(confidence_threshold=150)


def test_invalid_exclude_patterns_mode() -> None:
    with pytest.raises(ValueError):
        ReviewConfig.model_validate({"exclude_patterns_mode": "replaces"})


def test_valid_exclude_patterns_mode_accepted() -> None:
    cfg_append = ReviewConfig.model_validate({"exclude_patterns_mode": "append"})
    assert cfg_append.exclude_patterns_mode == "append"
    cfg_replace = ReviewConfig.model_validate({"exclude_patterns_mode": "replace"})
    assert cfg_replace.exclude_patterns_mode == "replace"


def test_exclude_patterns_mode_normalized_to_lowercase() -> None:
    """Mixed-case values like 'Replace' and 'APPEND' are accepted and stored lowercase."""
    cfg = ReviewConfig.model_validate({"exclude_patterns_mode": "Replace"})
    assert cfg.exclude_patterns_mode == "replace"
    cfg2 = ReviewConfig.model_validate({"exclude_patterns_mode": "APPEND"})
    assert cfg2.exclude_patterns_mode == "append"


def test_exclude_patterns_whitespace_trimmed_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI_EXCLUDE_PATTERNS trims whitespace around each comma-separated entry."""
    monkeypatch.setenv("AI_EXCLUDE_PATTERNS", "vendor/*, generated/*")
    cfg = ReviewConfig.from_env()
    assert cfg.exclude_patterns == ("vendor/*", "generated/*")


def test_all_ai_vars_read_in_from_env_are_known() -> None:
    """Every AI_* var consumed in from_env() must appear in _KNOWN_AI_VARS.

    Prevents a new AI_* var added to from_env() but forgotten in _KNOWN_AI_VARS
    from emitting a spurious unknown-variable warning for any user who sets it.
    """
    source = Path(_config_module.__file__).read_text()
    # Extract all "AI_..." string literals from the source
    ai_vars_in_source = set(re.findall(r'"(AI_[A-Z_]+)"', source))
    # Both _KNOWN_AI_VARS and _DEPRECATED_AI_VAR_ALIASES are valid homes
    # (internal vars like AI_AGENT are allowed extras in _KNOWN_AI_VARS)
    covered = _KNOWN_AI_VARS | set(_DEPRECATED_AI_VAR_ALIASES)
    missing = ai_vars_in_source - covered
    assert not missing, (
        f"AI_* vars referenced in config.py but missing from both "
        f"_KNOWN_AI_VARS and _DEPRECATED_AI_VAR_ALIASES: {missing}"
    )


def test_int_env_var_parse_failure_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "not-a-number")
    cfg = ReviewConfig.from_env()
    assert cfg.confidence_threshold == 75  # falls back to default
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "not-a-number" in captured.err
    assert "proceed with" in captured.err


def test_float_env_var_parse_failure_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_float() must warn on parse failure, mirroring _int().

    Without the warning, an operator who typos AI_TEMPERATURE silently
    gets the default and has no way to know their config was rejected.
    """
    monkeypatch.setenv("AI_TEMPERATURE", "not-a-float")
    cfg = ReviewConfig.from_env()
    assert cfg.temperature == 0.3  # falls back to default
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "not-a-float" in captured.err
    assert "proceed with" in captured.err


def test_cache_priming_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_CACHE_PRIMING unset → cache_priming defaults to False."""
    monkeypatch.delenv("AI_CACHE_PRIMING", raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.cache_priming is False


def test_cache_priming_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_CACHE_PRIMING=true → cache_priming is True."""
    monkeypatch.setenv("AI_CACHE_PRIMING", "true")
    cfg = ReviewConfig.from_env()
    assert cfg.cache_priming is True


def test_anthropic_premium_default_is_opus_4_8(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_models() should fill the Anthropic premium slot with claude-opus-4-8."""
    monkeypatch.delenv("AI_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("AI_MODEL_STANDARD", raising=False)
    cfg = ReviewConfig(provider="anthropic").resolve_models()
    assert cfg.model_premium == "claude-opus-4-8"


def test_anthropic_standard_default_is_sonnet_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_models() should fill the Anthropic standard slot with claude-sonnet-5."""
    monkeypatch.delenv("AI_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("AI_MODEL_STANDARD", raising=False)
    cfg = ReviewConfig(provider="anthropic").resolve_models()
    assert cfg.model_standard == "claude-sonnet-5"


def test_bedrock_proxy_standard_default_is_sonnet_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_models() should fill the bedrock-proxy standard slot with the Sonnet 5 ID."""
    monkeypatch.delenv("AI_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("AI_MODEL_STANDARD", raising=False)
    cfg = ReviewConfig(provider="bedrock-proxy").resolve_models()
    assert cfg.model_standard == "us.anthropic.claude-sonnet-5"


def test_context_enrichment_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Container image ships tree-sitter + ripgrep; enrichment should default on."""
    monkeypatch.delenv("AI_CONTEXT_ENRICHMENT", raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.enable_context_enrichment is True


def test_context_enrichment_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_CONTEXT_ENRICHMENT", "false")
    cfg = ReviewConfig.from_env()
    assert cfg.enable_context_enrichment is False


def test_bedrock_proxy_premium_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """bedrock-proxy premium stays on claude-opus-4-7 until Bedrock ID for 4.8 is confirmed."""
    monkeypatch.delenv("AI_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("AI_MODEL_STANDARD", raising=False)
    cfg = ReviewConfig(provider="bedrock-proxy").resolve_models()
    assert cfg.model_premium == "global.anthropic.claude-opus-4-7"


# ---------------------------------------------------------------------------
# #357: max_tokens_per_agent clamp
# ---------------------------------------------------------------------------

def test_max_tokens_per_agent_default() -> None:
    """Default value is 16384 (not 32768)."""
    cfg = ReviewConfig()
    assert cfg.max_tokens_per_agent == 16384


def test_max_tokens_per_agent_valid_passthrough() -> None:
    """Values inside [256, 65536] are stored as-is."""
    cfg = ReviewConfig(max_tokens_per_agent=8192)
    assert cfg.max_tokens_per_agent == 8192
    cfg2 = ReviewConfig(max_tokens_per_agent=65536)
    assert cfg2.max_tokens_per_agent == 65536
    cfg3 = ReviewConfig(max_tokens_per_agent=256)
    assert cfg3.max_tokens_per_agent == 256


def test_max_tokens_per_agent_clamp_too_low(capsys: pytest.CaptureFixture[str]) -> None:
    """Values below 256 are clamped to 256 with a warning."""
    cfg = ReviewConfig(max_tokens_per_agent=100)
    assert cfg.max_tokens_per_agent == 256
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "256" in captured.err


def test_max_tokens_per_agent_clamp_too_high(capsys: pytest.CaptureFixture[str]) -> None:
    """Values above 65536 are clamped to 65536 with a warning."""
    cfg = ReviewConfig(max_tokens_per_agent=99999)
    assert cfg.max_tokens_per_agent == 65536
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "65536" in captured.err


def test_max_tokens_per_agent_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env() reads 16384 when AI_MAX_TOKENS_PER_AGENT is unset."""
    monkeypatch.delenv("AI_MAX_TOKENS_PER_AGENT", raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.max_tokens_per_agent == 16384


def test_max_tokens_per_agent_env_clamp_low(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AI_MAX_TOKENS_PER_AGENT=50 is clamped to 256 via from_env()."""
    monkeypatch.setenv("AI_MAX_TOKENS_PER_AGENT", "50")
    cfg = ReviewConfig.from_env()
    assert cfg.max_tokens_per_agent == 256
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# Allow/deny selection: analyzers and agents
# ---------------------------------------------------------------------------


def test_analyzer_agent_allowdeny_defaults_are_empty_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All four allow/deny fields default to () when env vars are unset.

    Pins the 'all-unset = no filtering' contract: empty tuple means no
    allowlist/denylist in effect, so all eligible items run as before.
    """
    for var in ("AI_ANALYZERS", "AI_EXCLUDE_ANALYZERS", "AI_AGENTS", "AI_EXCLUDE_AGENTS"):
        monkeypatch.delenv(var, raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.analyzers == ()
    assert cfg.exclude_analyzers == ()
    assert cfg.agents == ()
    assert cfg.exclude_agents == ()


def test_analyzer_agent_allowdeny_empty_string_yields_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly empty-string env vars produce empty tuples (not ('',))."""
    monkeypatch.setenv("AI_ANALYZERS", "")
    monkeypatch.setenv("AI_EXCLUDE_ANALYZERS", "")
    monkeypatch.setenv("AI_AGENTS", "")
    monkeypatch.setenv("AI_EXCLUDE_AGENTS", "")
    cfg = ReviewConfig.from_env()
    assert cfg.analyzers == ()
    assert cfg.exclude_analyzers == ()
    assert cfg.agents == ()
    assert cfg.exclude_agents == ()


def test_analyzers_parsed_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_ANALYZERS is split on commas and whitespace is trimmed."""
    monkeypatch.setenv("AI_ANALYZERS", "semgrep, trufflehog")
    cfg = ReviewConfig.from_env()
    assert cfg.analyzers == ("semgrep", "trufflehog")


def test_exclude_analyzers_parsed_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_EXCLUDE_ANALYZERS is split on commas and whitespace is trimmed."""
    monkeypatch.setenv("AI_EXCLUDE_ANALYZERS", "checkov, tflint")
    cfg = ReviewConfig.from_env()
    assert cfg.exclude_analyzers == ("checkov", "tflint")


def test_agents_parsed_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_AGENTS is split on commas and whitespace is trimmed."""
    monkeypatch.setenv("AI_AGENTS", "code-reviewer, edge-case-hunter")
    cfg = ReviewConfig.from_env()
    assert cfg.agents == ("code-reviewer", "edge-case-hunter")


def test_exclude_agents_parsed_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_EXCLUDE_AGENTS is split on commas and whitespace is trimmed."""
    monkeypatch.setenv("AI_EXCLUDE_AGENTS", "adversarial-general, blind-hunter")
    cfg = ReviewConfig.from_env()
    assert cfg.exclude_agents == ("adversarial-general", "blind-hunter")


def test_unknown_analyzer_name_raises_with_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd analyzer name in AI_ANALYZERS fails at config load."""
    monkeypatch.setenv("AI_ANALYZERS", "semgrap")  # typo of 'semgrep'
    with pytest.raises((ValueError, Exception)) as exc_info:
        ReviewConfig.from_env()
    assert "semgrap" in str(exc_info.value)
    assert "semgrep" in str(exc_info.value)  # suggestion


def test_unknown_analyzer_in_denylist_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd name in the denylist (AI_EXCLUDE_ANALYZERS) also fails."""
    monkeypatch.setenv("AI_EXCLUDE_ANALYZERS", "tflent")  # typo of 'tflint'
    with pytest.raises((ValueError, Exception)) as exc_info:
        ReviewConfig.from_env()
    assert "tflent" in str(exc_info.value)


def test_unknown_agent_name_raises_with_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd agent name in AI_AGENTS fails at config load."""
    monkeypatch.setenv("AI_AGENTS", "edge-case-huntr")  # typo
    with pytest.raises((ValueError, Exception)) as exc_info:
        ReviewConfig.from_env()
    assert "edge-case-huntr" in str(exc_info.value)
    assert "edge-case-hunter" in str(exc_info.value)  # suggestion


def test_unknown_agent_in_denylist_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd name in the denylist (AI_EXCLUDE_AGENTS) also fails."""
    monkeypatch.setenv("AI_EXCLUDE_AGENTS", "adversarial-genral")  # typo
    with pytest.raises((ValueError, Exception)) as exc_info:
        ReviewConfig.from_env()
    assert "adversarial-genral" in str(exc_info.value)
