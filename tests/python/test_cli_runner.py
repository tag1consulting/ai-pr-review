"""Tests for the shared _cli_runner scaffold (issue #823).

The five analyzers that use `run_cli_json_analyzer` (eslint, shellcheck,
hadolint, ruff, kube-linter) exercise most of this indirectly through their
own test suites. These tests cover the scaffold's behavior directly,
including the `on_bad_returncode` hook (only eslint uses it today) and the
generic fallback message path no single analyzer's fixtures happen to hit.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native._cli_runner import run_cli_json_analyzer
from ai_pr_review.findings.models import Finding


def _finding(text: str = "x") -> Finding:
    return Finding(
        severity="Medium",
        confidence=90,
        source="test-tool",
        file="f.py",
        line=1,
        finding=text,
        remediation="fix it",
        category="lint",
    )


class TestSuccessPath:
    def test_items_converted_to_findings(self) -> None:
        with patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"items": [1, 2, 3]}', stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool", "--json"],
                extract_items=lambda data: data["items"],
                build_finding=lambda item: _finding(str(item)),
            )
        assert len(result) == 3
        assert [f.finding for f in result] == ["1", "2", "3"]

    def test_build_finding_returning_none_is_skipped_silently(self) -> None:
        with patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"items": [1, 2]}', stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data["items"],
                build_finding=lambda item: _finding() if item == 1 else None,
            )
        assert len(result) == 1

    def test_extract_items_returning_none_yields_no_findings(self) -> None:
        with patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='{"unexpected": true}', stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: None,
                build_finding=lambda item: _finding(),
            )
        assert result == []

    def test_empty_stdout_returns_empty(self) -> None:
        with patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data,
                build_finding=lambda item: _finding(),
            )
        assert result == []


class TestErrorHandling:
    def test_timeout_returns_empty_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch(
                "ai_pr_review.analyzers.native._cli_runner.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="test-tool", timeout=120),
            ),
            caplog.at_level("WARNING"),
        ):
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data,
                build_finding=lambda item: _finding(),
            )
        assert result == []
        assert "test-tool timed out" in caplog.text

    def test_oserror_returns_empty_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch(
                "ai_pr_review.analyzers.native._cli_runner.subprocess.run",
                side_effect=OSError("not found"),
            ),
            caplog.at_level("WARNING"),
        ):
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data,
                build_finding=lambda item: _finding(),
            )
        assert result == []
        assert "test-tool failed to start" in caplog.text

    def test_bad_returncode_uses_generic_message_by_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="boom")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data,
                build_finding=lambda item: _finding(),
            )
        assert result == []
        assert "test-tool exited 2" in caplog.text

    def test_on_bad_returncode_hook_replaces_generic_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[int] = []

        def custom(result: subprocess.CompletedProcess[str]) -> None:
            calls.append(result.returncode)

        with (
            patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="boom")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data,
                build_finding=lambda item: _finding(),
                on_bad_returncode=custom,
            )
        assert result == []
        assert calls == [2]
        assert "test-tool exited 2" not in caplog.text

    def test_non_json_stdout_returns_empty_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data,
                build_finding=lambda item: _finding(),
            )
        assert result == []
        assert "test-tool produced non-JSON output" in caplog.text

    def test_malformed_item_dropped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        def exploding_build(item: object) -> Finding:
            raise ValueError("bad severity")

        with (
            patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"items": [1]}', stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data["items"],
                build_finding=exploding_build,
            )
        assert result == []
        assert "test-tool dropped malformed finding" in caplog.text

    def test_custom_success_returncodes(self) -> None:
        """A tool whose 'success' set differs from the {0, 1} default."""
        with patch("ai_pr_review.analyzers.native._cli_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3, stdout='{"items": [1]}', stderr="")
            result = run_cli_json_analyzer(
                tool="test-tool",
                command=["test-tool"],
                extract_items=lambda data: data["items"],
                build_finding=lambda item: _finding(),
                success_returncodes=frozenset({0, 1, 2, 3}),
            )
        assert len(result) == 1
