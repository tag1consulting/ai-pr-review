"""Tests for ai_pr_review.review.preflight.fetch_pr_intent / build_pr_intent_addendum.

Mirrors tests/python/test_issue_linker.py's TestFetchOpenIssues pattern
(same gh-CLI subprocess conventions, same fail-soft contract).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.review.preflight import build_pr_intent_addendum, fetch_pr_intent


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    proc: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# fetch_pr_intent
# ---------------------------------------------------------------------------


class TestFetchPrIntent:
    def test_success_returns_title_and_body(self) -> None:
        payload = json.dumps({"title": "Fix the widget", "body": "Closes #42."})
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = fetch_pr_intent("7", "owner/repo")
        assert "Title: Fix the widget" in result
        assert "Closes #42." in result

    def test_no_pr_number_returns_empty(self) -> None:
        result = fetch_pr_intent("", "owner/repo")
        assert result == ""

    def test_title_only_no_body(self) -> None:
        payload = json.dumps({"title": "Fix the widget", "body": ""})
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == "Title: Fix the widget"

    def test_empty_title_and_body_returns_empty(self) -> None:
        payload = json.dumps({"title": "", "body": ""})
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == ""

    def test_body_truncated_beyond_max_chars(self) -> None:
        long_body = "x" * 5000
        payload = json.dumps({"title": "t", "body": long_body})
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = fetch_pr_intent("7", "owner/repo")
        assert "[...truncated...]" in result
        assert len(result) < 4200

    def test_gh_absent_returns_empty(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == ""

    def test_timeout_returns_empty(self) -> None:
        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15)
        ):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == ""

    def test_nonzero_exit_returns_empty(self) -> None:
        with patch(
            "subprocess.run", return_value=_completed(returncode=1, stderr="auth error")
        ):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == ""

    def test_bad_json_returns_empty(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout="not-json")):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == ""

    def test_permission_error_returns_empty(self) -> None:
        """OSError (and subclasses, e.g. PermissionError) are the expected failure
        shape for a subprocess call and must fail soft, unlike a genuine
        programming error -- see test_programming_error_propagates below."""
        with patch("subprocess.run", side_effect=PermissionError("denied")):
            result = fetch_pr_intent("7", "owner/repo")
        assert result == ""

    def test_programming_error_propagates(self) -> None:
        """A bug in this function's own code (not a subprocess/OS failure) must
        not be silently swallowed as "PR intent unavailable" -- the except
        clause is narrowed to (OSError, subprocess.SubprocessError) specifically
        so this doesn't happen (see the same reasoning as runtime.py's
        except ImportError: raise for the module import itself)."""
        with (
            patch("subprocess.run", side_effect=RuntimeError("unexpected")),
            pytest.raises(RuntimeError),
        ):
            fetch_pr_intent("7", "owner/repo")


# ---------------------------------------------------------------------------
# build_pr_intent_addendum
# ---------------------------------------------------------------------------


class TestBuildPrIntentAddendum:
    def test_wraps_text_in_pr_intent_block(self) -> None:
        addendum = build_pr_intent_addendum("Title: Fix the widget")
        assert addendum.startswith("<pr-intent>")
        assert addendum.endswith("</pr-intent>")
        assert "Title: Fix the widget" in addendum

    def test_empty_text_returns_empty(self) -> None:
        assert build_pr_intent_addendum("") == ""
