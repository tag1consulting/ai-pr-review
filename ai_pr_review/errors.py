"""Shared exception hierarchy for ai-pr-review.

All exceptions are subclasses of ``AiPrReviewError`` so callers can catch
the base class when they want to handle any engine failure, or catch a
specific subclass for narrower handling.

Note: this module defines base classes only.  It does NOT replace existing
internal exceptions (e.g. ``_ConflictError``, ``_MissingBranchError`` in
``feedback/store.py``) — those remain module-private.
"""

from __future__ import annotations


class AiPrReviewError(Exception):
    """Base class for all ai-pr-review errors."""


class ConfigError(AiPrReviewError):
    """Bad configuration or missing required environment variable."""


class EngineError(AiPrReviewError):
    """Compute-layer failure (diff read, agent dispatch, findings pipeline)."""


class ProviderError(AiPrReviewError):
    """LLM or VCS API failure."""


class AnalyzerError(AiPrReviewError):
    """Static analyzer subprocess failure."""


class CapabilityError(AiPrReviewError):
    """Optional capability failure (tree-sitter, SARIF ingestion, feedback store).

    These are fail-soft: a ``CapabilityError`` should be caught, logged as a
    WARNING, and execution should continue without the capability.
    """
