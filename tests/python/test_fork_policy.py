"""The neutral fork policy module and its compatibility re-exports."""

from __future__ import annotations

import subprocess
import sys

import pytest

from ai_pr_review import fork_policy
from ai_pr_review.review import fork_gate

_NAMES = (
    "DEFAULT_FORK_MAX_COST_USD", "DEFAULT_FORK_POLICY", "DEFAULT_TRUSTED_ASSOCIATIONS",
    "FORK_POLICIES", "SAFEST_FORK_POLICY", "parse_trusted_associations",
)


@pytest.mark.parametrize("name", _NAMES)
def test_fork_gate_re_exports_the_policy_names(name: str) -> None:
    assert getattr(fork_gate, name) is getattr(fork_policy, name)


@pytest.mark.parametrize("module", ["ai_pr_review.fork_policy", "ai_pr_review.config"])
def test_the_module_imports_alone_without_review_or_vcs(module: str) -> None:
    code = (
        f"import sys, {module}\n"
        "bad = [m for m in sys.modules if m.startswith(('ai_pr_review.review', 'ai_pr_review.vcs'))]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", [
    "ai_pr_review.review.fork_gate", "ai_pr_review.vcs.github", "ai_pr_review.review.runtime",
    "ai_pr_review.review.fork_budget", "ai_pr_review.review.fork_spend",
])
def test_each_fork_module_imports_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
