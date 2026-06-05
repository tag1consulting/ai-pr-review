"""Tests for issue-linker open-issue pre-fetch and user message assembly.

Covers:
- _fetch_open_issues: success, timeout, non-zero exit, gh absent, bad JSON, empty list
- _run_issue_linker: user message includes ## Open Issues block; degrades gracefully
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest

from ai_pr_review.cli import _fetch_open_issues, _run_issue_linker
from ai_pr_review.llm.base import LLMRequest, LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, input_tokens=10, output_tokens=20)


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    proc: subprocess.CompletedProcess[str] = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# _fetch_open_issues
# ---------------------------------------------------------------------------


class TestFetchOpenIssues:
    def test_success_formats_issues(self) -> None:
        payload = json.dumps([
            {"number": 10, "title": "Fix the widget", "labels": [{"name": "bug"}]},
            {"number": 20, "title": "Add dark mode", "labels": []},
        ])
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = _fetch_open_issues("owner/repo")
        assert "#10 Fix the widget [bug]" in result
        assert "#20 Add dark mode" in result

    def test_empty_list_returns_sentinel(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout="[]")):
            result = _fetch_open_issues("owner/repo")
        assert result == "(no open issues)"

    def test_gh_absent_returns_unavailable(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = _fetch_open_issues("owner/repo")
        assert result == "(unavailable)"

    def test_timeout_returns_unavailable(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=15),
        ):
            result = _fetch_open_issues("owner/repo")
        assert result == "(unavailable)"

    def test_nonzero_exit_returns_unavailable(self) -> None:
        with patch(
            "subprocess.run", return_value=_completed(returncode=1, stderr="auth error")
        ):
            result = _fetch_open_issues("owner/repo")
        assert result == "(unavailable)"

    def test_bad_json_returns_unavailable(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout="not-json")):
            result = _fetch_open_issues("owner/repo")
        assert result == "(unavailable)"

    def test_unexpected_exception_returns_unavailable(self) -> None:
        with patch("subprocess.run", side_effect=RuntimeError("unexpected")):
            result = _fetch_open_issues("owner/repo")
        assert result == "(unavailable)"

    def test_issue_without_labels(self) -> None:
        payload = json.dumps([{"number": 5, "title": "Bare issue", "labels": []}])
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = _fetch_open_issues("owner/repo")
        assert result == "#5 Bare issue"
        assert "[" not in result

    def test_multiple_labels(self) -> None:
        payload = json.dumps([{
            "number": 7,
            "title": "Multi-label",
            "labels": [{"name": "enhancement"}, {"name": "help wanted"}],
        }])
        with patch("subprocess.run", return_value=_completed(stdout=payload)):
            result = _fetch_open_issues("owner/repo")
        assert "[enhancement, help wanted]" in result


# ---------------------------------------------------------------------------
# _run_issue_linker user message assembly
# ---------------------------------------------------------------------------


class TestRunIssueLinkerUserMessage:
    """Assert that the user message sent to the LLM contains an ## Open Issues block."""

    @pytest.fixture()
    def prompt_dir(self, tmp_path: Path) -> Path:
        prompt = tmp_path / "prompts" / "issue-linker.md"
        prompt.parent.mkdir()
        prompt.write_text("You are a test prompt.\n")
        return tmp_path

    def _run(self, prompt_dir: Path, mock_run_side_effects: list, llm_text: str = "NONE") -> tuple[str, list[LLMRequest]]:
        """Run _run_issue_linker under anyio.run with mocked subprocess and llm_call."""
        captured: list[LLMRequest] = []

        async def _fake_llm(req: LLMRequest) -> LLMResponse:
            captured.append(req)
            return _make_response(llm_text)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = mock_run_side_effects
            result = anyio.run(
                lambda: _run_issue_linker(
                    manifest_text="## Manifest\n- src/foo.py",
                    base_ref="main",
                    script_dir=prompt_dir,
                    provider="github",
                    github_repository="owner/repo",
                    model="claude-haiku-4-5",
                    llm_call=_fake_llm,
                )
            )

        return result, captured

    def test_open_issues_block_injected_on_success(self, prompt_dir: Path) -> None:
        issue_payload = json.dumps([{"number": 42, "title": "A real issue", "labels": []}])
        _result, captured = self._run(
            prompt_dir,
            mock_run_side_effects=[
                _completed(stdout="abc1234 fix: test commit\n"),  # git log
                _completed(stdout="feat/test-branch"),             # git rev-parse
                _completed(stdout=issue_payload),                  # gh issue list
            ],
        )
        assert len(captured) == 1, "Expected one LLM call"
        user_msg = captured[0].user_message
        assert "## Open Issues" in user_msg
        assert "#42 A real issue" in user_msg

    def test_open_issues_unavailable_does_not_abort(self, prompt_dir: Path) -> None:
        """When gh is absent the LLM call still proceeds with (unavailable)."""
        _result, captured = self._run(
            prompt_dir,
            mock_run_side_effects=[
                _completed(stdout="abc1234 fix: test commit\n"),  # git log
                _completed(stdout="feat/test-branch"),             # git rev-parse
                FileNotFoundError("gh not found"),                 # gh issue list
            ],
        )
        assert len(captured) == 1, "LLM call must proceed even when gh is absent"
        user_msg = captured[0].user_message
        assert "## Open Issues" in user_msg
        assert "(unavailable)" in user_msg

    def test_llm_none_sentinel_returns_empty_string(self, prompt_dir: Path) -> None:
        """When the LLM returns NONE, _run_issue_linker returns ""."""
        result, _captured = self._run(
            prompt_dir,
            mock_run_side_effects=[
                _completed(stdout="abc1234 fix: test\n"),
                _completed(stdout="feat/branch"),
                _completed(stdout="[]"),
            ],
            llm_text="NONE",
        )
        assert result == ""

    def test_run_issue_linker_never_raises(self, prompt_dir: Path) -> None:
        """Catastrophic failure in the LLM call must be swallowed (fail-soft)."""
        captured: list[LLMRequest] = []

        async def _exploding_llm(req: LLMRequest) -> LLMResponse:
            captured.append(req)
            raise RuntimeError("simulated LLM failure")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(stdout="abc1234 fix: something\n"),
                _completed(stdout="feat/branch"),
                _completed(stdout="[]"),
            ]
            result = anyio.run(
                lambda: _run_issue_linker(
                    manifest_text="## Manifest",
                    base_ref="main",
                    script_dir=prompt_dir,
                    provider="github",
                    github_repository="owner/repo",
                    model="claude-haiku-4-5",
                    llm_call=_exploding_llm,
                )
            )

        assert result == ""  # fail-soft: empty string, not an exception
