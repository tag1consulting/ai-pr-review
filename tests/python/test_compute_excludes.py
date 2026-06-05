"""Tests for _resolve_excludes() in ai_pr_review.diff.compute."""

from __future__ import annotations

import logging

import pytest

from ai_pr_review.diff.compute import _EXCLUDE_PATTERNS, _resolve_excludes


class TestResolveExcludes:
    def test_no_user_patterns_append_returns_builtins(self) -> None:
        """Empty user patterns with 'append' mode returns exactly the built-in list."""
        result = _resolve_excludes((), "append")
        assert result == list(_EXCLUDE_PATTERNS)

    def test_user_pattern_append_adds_after_builtins(self) -> None:
        """A user pattern in 'append' mode is appended after the built-in list."""
        result = _resolve_excludes(("docs/*",), "append")
        assert result == list(_EXCLUDE_PATTERNS) + [":!docs/*"]

    def test_already_prefixed_pattern_not_doubled(self) -> None:
        """Patterns that already start with ':!' are not double-prefixed."""
        result = _resolve_excludes((":!already-prefixed/*",), "append")
        assert result == list(_EXCLUDE_PATTERNS) + [":!already-prefixed/*"]

    def test_replace_mode_with_patterns_drops_builtins(self) -> None:
        """'replace' mode with user patterns returns only those patterns, no built-ins."""
        result = _resolve_excludes(("vendor/other/*",), "replace")
        assert result == [":!vendor/other/*"]
        # Verify none of the built-ins leaked in.
        for builtin in _EXCLUDE_PATTERNS:
            assert builtin not in result

    def test_replace_mode_empty_falls_back_to_builtins_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'replace' with no patterns falls back to built-ins and logs a warning."""
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.diff.compute"):
            result = _resolve_excludes((), "replace")

        assert result == list(_EXCLUDE_PATTERNS)
        assert any("replace" in record.message for record in caplog.records), (
            "Expected a warning mentioning 'replace' in the log output"
        )

    def test_multiple_user_patterns_append(self) -> None:
        """Multiple user patterns are all normalized and appended."""
        result = _resolve_excludes(("docs/*", "*.generated.go"), "append")
        assert result == list(_EXCLUDE_PATTERNS) + [":!docs/*", ":!*.generated.go"]

    def test_multiple_user_patterns_replace(self) -> None:
        """Multiple user patterns in replace mode — only those patterns returned."""
        result = _resolve_excludes(("custom/a/*", ":!custom/b/*"), "replace")
        assert result == [":!custom/a/*", ":!custom/b/*"]
        assert len(result) == 2
