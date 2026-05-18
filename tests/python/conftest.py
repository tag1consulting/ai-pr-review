"""Session-wide pytest fixtures for the ai_pr_review test suite."""

from __future__ import annotations

import logging
from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def _reset_pkg_logger() -> Generator[None, None, None]:
    """Reset the ai_pr_review package logger before and after every test.

    setup_logging() installs a handler on logging.getLogger('ai_pr_review')
    and sets propagate=False.  Without cleanup, a test that calls setup_logging
    leaves the logger in that state for the rest of the session — breaking
    caplog-based tests in other files that rely on propagation to the root logger.
    Managed handlers are closed before being discarded to release file descriptors.
    """
    pkg = logging.getLogger("ai_pr_review")
    original_handlers = pkg.handlers[:]
    original_level = pkg.level
    original_propagate = pkg.propagate
    yield
    for h in pkg.handlers:
        if getattr(h, "_ai_pr_review_managed", False) and h not in original_handlers:
            h.close()
    pkg.handlers[:] = original_handlers
    pkg.setLevel(original_level)
    pkg.propagate = original_propagate
