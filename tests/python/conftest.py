"""Session-wide pytest fixtures for the ai_pr_review test suite."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_pkg_logger() -> None:  # type: ignore[return]
    """Reset the ai_pr_review package logger before and after every test.

    setup_logging() installs a handler on logging.getLogger('ai_pr_review')
    and sets propagate=False.  Without cleanup, a test that calls setup_logging
    leaves the logger in that state for the rest of the session — breaking
    caplog-based tests in other files that rely on propagation to the root logger.
    """
    pkg = logging.getLogger("ai_pr_review")
    original_handlers = pkg.handlers[:]
    original_level = pkg.level
    original_propagate = pkg.propagate
    yield
    pkg.handlers[:] = original_handlers
    pkg.setLevel(original_level)
    pkg.propagate = original_propagate
