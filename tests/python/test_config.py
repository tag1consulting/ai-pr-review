"""Tests for ai_pr_review.config (S3 — typed config)."""

import os
import re
from pathlib import Path

import pytest

import ai_pr_review.config as _config_module
from ai_pr_review.config import _KNOWN_AI_VARS, ConfigError, ReviewConfig


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
    assert cfg.engine == "bash"
    assert cfg.confidence_threshold == 75
    assert cfg.max_diff_lines == 5000
    assert cfg.parallel is True


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
    # _KNOWN_AI_VARS must be a superset (internal vars like AI_AGENT are allowed extras)
    missing = ai_vars_in_source - _KNOWN_AI_VARS
    assert not missing, (
        f"AI_* vars referenced in config.py but missing from _KNOWN_AI_VARS: {missing}"
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
