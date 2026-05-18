"""Tests for ai_pr_review/errors.py — exception hierarchy."""

from __future__ import annotations

import pytest


def test_exception_hierarchy():
    from ai_pr_review.errors import (
        AiPrReviewError,
        AnalyzerError,
        CapabilityError,
        ConfigError,
        EngineError,
        ProviderError,
    )

    assert issubclass(ConfigError, AiPrReviewError)
    assert issubclass(EngineError, AiPrReviewError)
    assert issubclass(ProviderError, AiPrReviewError)
    assert issubclass(AnalyzerError, AiPrReviewError)
    assert issubclass(CapabilityError, AiPrReviewError)
    assert issubclass(AiPrReviewError, Exception)


def test_capability_error_catchable_as_base():
    from ai_pr_review.errors import AiPrReviewError, CapabilityError

    with pytest.raises(AiPrReviewError):
        raise CapabilityError("tree-sitter unavailable")
