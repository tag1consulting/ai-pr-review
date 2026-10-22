"""Session-wide pytest fixtures for the ai_pr_review test suite."""

from __future__ import annotations

import logging
from collections.abc import Generator

import pytest

import ai_pr_review.logging as _logging_module


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
    original_literals = _logging_module._active_secret_literals
    yield
    _logging_module._active_secret_literals = original_literals
    for h in pkg.handlers:
        if getattr(h, "_ai_pr_review_managed", False) and h not in original_handlers:
            h.close()
    pkg.handlers[:] = original_handlers
    pkg.setLevel(original_level)
    pkg.propagate = original_propagate


@pytest.fixture(autouse=True)
def _reset_fork_isolation_state():
    """Fork isolation and passthrough are run-wide module state: never let one test leak it."""
    from ai_pr_review.analyzers import _env

    _env.configure_fork_isolation(False)
    _env.configure_passthrough((), allow_passthrough=True)
    yield
    _env.configure_fork_isolation(False)
    _env.configure_passthrough((), allow_passthrough=True)
