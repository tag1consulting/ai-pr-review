"""Tests for ai_pr_review.config (S3 — typed config)."""

import os
import re
from pathlib import Path

import pytest

import ai_pr_review.config as _config_module
from ai_pr_review.config import (
    _DEPRECATED_AI_VAR_ALIASES,
    _KNOWN_AI_VARS,
    ConfigError,
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
    assert cfg.engine == "python"
    assert cfg.confidence_threshold == 75
    assert cfg.max_diff_lines == 5000
    assert cfg.parallel is True


def test_engine_field_default() -> None:
    """ReviewConfig() bare constructor should default engine to 'python'."""
    cfg = ReviewConfig()
    assert cfg.engine == "python"


def test_override_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_REVIEW_MODE", "full")
    monkeypatch.setenv("AI_PR_REVIEW_ENGINE", "python")
    monkeypatch.setenv("AI_CONFIDENCE_THRESHOLD", "50")
    cfg = ReviewConfig.from_env()
    assert cfg.review_mode == "full"
    assert cfg.engine == "python"
    assert cfg.confidence_threshold == 50


def test_unknown_ai_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_TOTALLY_MADE_UP", "1")
    with pytest.raises(ConfigError, match="AI_TOTALLY_MADE_UP"):
        ReviewConfig.from_env()


def test_unknown_ai_var_typo_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_REVIEW_MDOE", "quick")  # typo: MDOE vs MODE
    with pytest.raises(ConfigError, match="AI_REVIEW_MODE"):
        ReviewConfig.from_env()


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


def test_invalid_engine() -> None:
    with pytest.raises(ValueError):
        ReviewConfig.model_validate({"engine": "ruby"})


def test_invalid_confidence_threshold() -> None:
    with pytest.raises(ValueError):
        ReviewConfig(confidence_threshold=150)


def test_all_ai_vars_read_in_from_env_are_known() -> None:
    """Every AI_* var consumed in from_env() must appear in _KNOWN_AI_VARS.

    Prevents the silent ConfigError trap: a new AI_* var added to from_env()
    but forgotten in _KNOWN_AI_VARS would raise ConfigError for any user who
    sets that var.
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


def test_bedrock_proxy_premium_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """bedrock-proxy premium stays on claude-opus-4-7 until Bedrock ID for 4.8 is confirmed."""
    monkeypatch.delenv("AI_MODEL_PREMIUM", raising=False)
    monkeypatch.delenv("AI_MODEL_STANDARD", raising=False)
    cfg = ReviewConfig(provider="bedrock-proxy").resolve_models()
    assert cfg.model_premium == "global.anthropic.claude-opus-4-7"
