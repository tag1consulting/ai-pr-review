"""Tests for tests/canary/live_model_canary.py's failure classification.

Filed alongside the #636 fix: the live-model canary's GitHub issue body used to
unconditionally claim every failure was "the same class as #592" (a model-behavior
regression). #636 itself was an Anthropic workspace API usage limit, not a model
bug, and the hardcoded text sent whoever triaged it chasing the wrong root cause.
These tests cover the classification and output-writing logic that now lets the
workflow tell the two apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/ has no __init__.py, so tests.canary isn't importable as a regular
# package by name alone -- add the repo root to sys.path first, same as
# live_model_canary.py does for its own ai_pr_review imports.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.canary.live_model_canary import (  # noqa: E402
    CanaryResult,
    _is_quota_error,
    _write_github_output,
)


def _parse_github_output_multiline(content: str, key: str) -> str:
    """Parse a `key<<DELIM\\n...\\nDELIM\\n` block the way GitHub Actions does.

    Mirrors the runner's actual behavior: the value ends at the first line that
    matches the opening delimiter verbatim, whatever that delimiter is. Raises
    AssertionError if the key isn't present or the block is malformed, so a
    delimiter collision (the F2 finding this guards against) fails loudly
    instead of silently truncating.
    """
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}<<"):
            delimiter = line[len(f"{key}<<") :]
            for j in range(i + 1, len(lines)):
                if lines[j] == delimiter:
                    return "\n".join(lines[i + 1 : j])
            raise AssertionError(f"no closing delimiter {delimiter!r} found for key {key!r}")
    raise AssertionError(f"key {key!r} not found in GITHUB_OUTPUT content")


class TestIsQuotaError:
    def test_workspace_usage_limit_is_quota(self) -> None:
        detail = (
            "agent failed (exit_code=1): SystemExit: 1 | caused by LLMError: "
            'Anthropic returned HTTP 400: {"type":"error","error":{"type":'
            '"invalid_request_error","message":"You have reached your specified '
            'workspace API usage limits. You will regain access on 2026-08-01 '
            'at 00:00 UTC."}}'
        )
        assert _is_quota_error(detail)

    def test_rate_limit_error_is_quota(self) -> None:
        assert _is_quota_error("rate_limit_error: too many requests")

    def test_low_credit_balance_is_quota(self) -> None:
        assert _is_quota_error("Your credit balance is too low to access the API.")

    def test_stop_reason_anomaly_is_not_quota(self) -> None:
        # The actual #592 shape: a real model-behavior surprise, not a billing block.
        detail = "stop_reason='max_tokens' (expected end_turn), thinking_tokens=16384, output_tokens=0"
        assert not _is_quota_error(detail)

    def test_unexpected_exception_is_not_quota(self) -> None:
        assert not _is_quota_error("run_tier raised: TimeoutError()")

    def test_case_insensitive(self) -> None:
        assert _is_quota_error("USAGE LIMIT reached")


class TestWriteGithubOutput:
    def test_noop_without_github_output_env(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        # Must not raise even with failures present and no env var set.
        _write_github_output([CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, "usage limit")])

    def test_noop_with_no_failures(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        _write_github_output([])
        assert out_path.read_text() == ""

    def test_all_quota_exhausted_true(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        failed = [
            CanaryResult("anthropic", "claude-opus-4-8", "code-reviewer", False, "usage limit reached"),
            CanaryResult("anthropic", "claude-sonnet-5", "silent-failure-hunter", False, "usage limit reached"),
        ]
        _write_github_output(failed)
        content = out_path.read_text()
        assert "all_quota_exhausted=true" in content
        detail_value = _parse_github_output_multiline(content, "failure_detail")
        assert "anthropic/claude-opus-4-8/code-reviewer: usage limit reached" in detail_value

    def test_delimiter_is_random_per_call(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        result = [CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, "usage limit")]

        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        _write_github_output(result)
        first_delimiter = next(
            line[len("failure_detail<<") :]
            for line in out_path.read_text().splitlines()
            if line.startswith("failure_detail<<")
        )

        out_path.write_text("")
        _write_github_output(result)
        second_delimiter = next(
            line[len("failure_detail<<") :]
            for line in out_path.read_text().splitlines()
            if line.startswith("failure_detail<<")
        )

        assert first_delimiter != second_delimiter

    def test_failure_detail_containing_old_fixed_delimiter_does_not_truncate(self, tmp_path, monkeypatch) -> None:
        # F2 regression: a fixed delimiter could appear inside provider error
        # text/tracebacks and silently truncate the value. Embed the literal
        # string this script used to hardcode as the delimiter, inside the
        # failure detail itself, and confirm parsing still recovers it whole
        # rather than cutting off at that line.
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        poisoned_detail = "anthropic/claude-sonnet-5/code-reviewer: error containing CANARY_FAILURE_EOF verbatim"
        _write_github_output([CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, poisoned_detail)])

        detail_value = _parse_github_output_multiline(out_path.read_text(), "failure_detail")
        assert poisoned_detail in detail_value

    def test_mixed_failures_not_all_quota(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        failed = [
            CanaryResult("anthropic", "claude-opus-4-8", "code-reviewer", False, "stop_reason='max_tokens'"),
            CanaryResult("anthropic", "claude-sonnet-5", "silent-failure-hunter", False, "usage limit reached"),
        ]
        _write_github_output(failed)
        assert "all_quota_exhausted=false" in out_path.read_text()

    def test_appends_rather_than_overwrites(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("existing_output=1\n")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        _write_github_output([CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, "usage limit")])
        content = out_path.read_text()
        assert content.startswith("existing_output=1\n")
        assert "all_quota_exhausted=true" in content
