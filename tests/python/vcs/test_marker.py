"""Tests for ai_pr_review.vcs.marker."""

from __future__ import annotations

import pytest

from ai_pr_review.vcs.marker import (
    INLINE_MARKER,
    SUMMARY_MARKER_PREFIX,
    append_inline_marker,
    build_summary_marker,
    extract_summary_sha,
    has_inline_marker,
    has_summary_marker,
    replace_summary_sha,
)

_VALID_SHA = "abc123def4567890abc123def4567890abc123de"


# ---------------------------------------------------------------------------
# build_summary_marker
# ---------------------------------------------------------------------------

def test_build_summary_marker_with_valid_sha() -> None:
    marker = build_summary_marker(_VALID_SHA)
    assert marker == f"<!-- ai-pr-review-summary sha={_VALID_SHA} -->"


def test_build_summary_marker_short_sha_accepted() -> None:
    marker = build_summary_marker("abc1234")
    assert "sha=abc1234" in marker


def test_build_summary_marker_without_sha() -> None:
    marker = build_summary_marker("")
    assert marker == "<!-- ai-pr-review-summary -->"


def test_build_summary_marker_invalid_sha_drops_field() -> None:
    marker = build_summary_marker("not-a-sha")
    assert "sha=" not in marker
    assert marker == "<!-- ai-pr-review-summary -->"


def test_build_summary_marker_trailing_newline_rejected() -> None:
    # `$` regex anchor allows trailing \n by default; \A...\Z rejects it.
    marker = build_summary_marker(_VALID_SHA + "\n")
    assert "\n" not in marker
    assert marker == "<!-- ai-pr-review-summary -->"


def test_replace_summary_sha_trailing_newline_is_noop() -> None:
    body = "<!-- ai-pr-review-summary sha=abc1234 -->"
    result = replace_summary_sha(body, _VALID_SHA + "\n")
    assert result == body  # invalid new_sha: no change


def test_extract_summary_sha_context_hint_included_in_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Use a too-short hex SHA (6 chars) — matches the regex capture but fails
    # the length check in _is_valid_sha, triggering the warning path.
    body = "<!-- ai-pr-review-summary sha=abcdef -->"
    result = extract_summary_sha(body, context_hint="comment-id=12345")
    assert result is None
    captured = capsys.readouterr()
    assert "comment-id=12345" in captured.err


def test_extract_summary_sha_falls_back_to_body_excerpt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = "some long body with\nnewlines <!-- ai-pr-review-summary sha=abcdef -->"
    extract_summary_sha(body)
    captured = capsys.readouterr()
    assert "some long body with" in captured.err


def test_replace_summary_sha_context_hint_included_in_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    replace_summary_sha("no marker", _VALID_SHA, context_hint="pr=274")
    captured = capsys.readouterr()
    assert "pr=274" in captured.err


# ---------------------------------------------------------------------------
# extract_summary_sha
# ---------------------------------------------------------------------------

def test_extract_summary_sha_happy_path() -> None:
    body = f"<!-- ai-pr-review-summary sha={_VALID_SHA} -->\n\n## Summary text"
    assert extract_summary_sha(body) == _VALID_SHA


def test_extract_summary_sha_embedded_in_body() -> None:
    body = "Some prefix\n<!-- ai-pr-review-summary sha=abc1234 -->\nmore text"
    assert extract_summary_sha(body) == "abc1234"


def test_extract_summary_sha_no_marker() -> None:
    assert extract_summary_sha("plain body with no marker") is None


def test_extract_summary_sha_marker_without_sha_field() -> None:
    body = "<!-- ai-pr-review-summary -->"
    assert extract_summary_sha(body) is None


def test_extract_summary_sha_rejects_malformed_sha() -> None:
    body = "<!-- ai-pr-review-summary sha=not-hex! -->"
    assert extract_summary_sha(body) is None


def test_extract_summary_sha_round_trips_build_result() -> None:
    marker = build_summary_marker(_VALID_SHA)
    body = f"{marker}\n\nbody content"
    assert extract_summary_sha(body) == _VALID_SHA


# ---------------------------------------------------------------------------
# has_inline_marker / has_summary_marker
# ---------------------------------------------------------------------------

def test_has_inline_marker_detects() -> None:
    body = f"comment text\n{INLINE_MARKER}"
    assert has_inline_marker(body) is True


def test_has_inline_marker_rejects_empty() -> None:
    assert has_inline_marker("") is False


def test_has_inline_marker_rejects_plain_text() -> None:
    assert has_inline_marker("random body text") is False


def test_has_inline_marker_does_not_match_summary_marker() -> None:
    # Summary marker and inline marker are distinct strings
    summary_body = "<!-- ai-pr-review-summary sha=abc1234 -->"
    assert has_inline_marker(summary_body) is False


def test_has_summary_marker_detects_with_sha() -> None:
    body = "<!-- ai-pr-review-summary sha=abc1234 -->\ntext"
    assert has_summary_marker(body) is True


def test_has_summary_marker_detects_without_sha() -> None:
    assert has_summary_marker("<!-- ai-pr-review-summary -->") is True


def test_has_summary_marker_rejects_inline_marker() -> None:
    assert has_summary_marker(INLINE_MARKER) is False


def test_has_inline_marker_case_sensitive() -> None:
    # HTML comments are case-sensitive in practice; our checks match exactly.
    assert has_inline_marker("<!-- AI-PR-REVIEW-INLINE -->") is False


# ---------------------------------------------------------------------------
# append_inline_marker
# ---------------------------------------------------------------------------

def test_append_inline_marker_adds_to_plain_body() -> None:
    result = append_inline_marker("hello world")
    assert INLINE_MARKER in result
    assert "hello world" in result


def test_append_inline_marker_is_idempotent() -> None:
    once = append_inline_marker("body")
    twice = append_inline_marker(once)
    assert once == twice
    assert twice.count(INLINE_MARKER) == 1


def test_append_inline_marker_trailing_newline() -> None:
    result = append_inline_marker("body with trailing newline\n")
    assert result.endswith(INLINE_MARKER)
    # Body content preserved
    assert "body with trailing newline" in result


def test_append_inline_marker_no_trailing_newline_adds_separator() -> None:
    result = append_inline_marker("body")
    # Marker should be separated from body content, not jammed onto same line
    assert "body" in result
    assert INLINE_MARKER in result
    assert not result.startswith(INLINE_MARKER)  # body first
    # Ensure newline separation
    body_end = result.rfind(INLINE_MARKER) - 1
    assert result[body_end] == "\n"


def test_append_inline_marker_empty_body() -> None:
    result = append_inline_marker("")
    assert INLINE_MARKER in result


# ---------------------------------------------------------------------------
# replace_summary_sha
# ---------------------------------------------------------------------------

def test_replace_summary_sha_updates_existing_marker() -> None:
    old_sha = "abc1234"
    new_sha = "def5678"
    body = f"<!-- ai-pr-review-summary sha={old_sha} -->\n\n## Summary body"
    result = replace_summary_sha(body, new_sha)
    assert f"sha={new_sha}" in result
    assert f"sha={old_sha}" not in result
    assert "## Summary body" in result


def test_replace_summary_sha_preserves_surrounding_content() -> None:
    body = (
        "<!-- ai-pr-review-summary sha=abc1234 -->\n"
        "## Heading\n"
        "some body text with sha=abc1234 in it\n"  # unrelated mention
    )
    result = replace_summary_sha(body, "def5678")
    # Marker updated
    assert "<!-- ai-pr-review-summary sha=def5678 -->" in result
    # Body mention NOT touched — substring outside marker is preserved
    assert "some body text with sha=abc1234 in it" in result


def test_replace_summary_sha_noop_without_marker() -> None:
    body = "no marker here"
    result = replace_summary_sha(body, "abc1234")
    assert result == body


def test_replace_summary_sha_invalid_new_sha_is_noop() -> None:
    body = "<!-- ai-pr-review-summary sha=abc1234 -->"
    result = replace_summary_sha(body, "not-a-sha")
    assert result == body  # invalid SHA: no change


def test_replace_summary_sha_marker_without_sha_field_adds_sha() -> None:
    body = "<!-- ai-pr-review-summary -->\nbody"
    result = replace_summary_sha(body, "def5678")
    assert "<!-- ai-pr-review-summary sha=def5678 -->" in result


# ---------------------------------------------------------------------------
# Module-level constants are exported as expected
# ---------------------------------------------------------------------------

def test_inline_marker_constant() -> None:
    assert INLINE_MARKER == "<!-- ai-pr-review-inline -->"


def test_summary_marker_prefix_constant() -> None:
    assert SUMMARY_MARKER_PREFIX == "<!-- ai-pr-review-summary"
