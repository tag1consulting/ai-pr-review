"""Tests for ai_pr_review.config (S3 — typed config)."""

import os

import pytest

from ai_pr_review.config import ConfigError, ReviewConfig


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
    with pytest.raises(Exception):
        ReviewConfig(review_mode="invalid")  # type: ignore[arg-type]


def test_invalid_engine() -> None:
    with pytest.raises(Exception):
        ReviewConfig(engine="ruby")  # type: ignore[arg-type]


def test_invalid_confidence_threshold() -> None:
    with pytest.raises(Exception):
        ReviewConfig(confidence_threshold=150)
