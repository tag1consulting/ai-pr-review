"""Tests for ai_pr_review.vcs.marker."""

from __future__ import annotations

import pytest

from ai_pr_review.vcs.marker import (
    INLINE_MARKER,
    INLINE_MARKER_HIDDEN,
    SKIP_MARKER,
    SKIP_MARKER_HIDDEN,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SUMMARY_MARKER_PREFIX,
    append_inline_marker,
    append_skip_marker,
    build_id_map_marker,
    build_summary_marker,
    build_verdicts_marker,
    extract_id_map,
    extract_summary_sha,
    extract_verdicts,
    has_inline_marker,
    has_skip_marker,
    has_summary_marker,
    replace_summary_sha,
    upsert_verdicts_marker,
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


def test_replace_summary_sha_invalid_sha_warning_includes_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = "<!-- ai-pr-review-summary sha=abc1234 -->"
    replace_summary_sha(body, "not-a-sha", context_hint="pr=999")
    captured = capsys.readouterr()
    # Both pieces of context should be in the warning
    assert "not-a-sha" in captured.err
    assert "pr=999" in captured.err


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


# ---------------------------------------------------------------------------
# Hidden (reference-link) marker form — Bitbucket's renderer HTML-escapes
# raw `<!-- -->` comments instead of hiding them (#699); these call sites use
# the `[//]: # (...)` form instead, which all three providers render as
# nothing.
# ---------------------------------------------------------------------------


def test_hidden_marker_constants() -> None:
    assert INLINE_MARKER_HIDDEN == "[//]: # (ai-pr-review-inline)"
    assert SKIP_MARKER_HIDDEN == "[//]: # (ai-pr-review-skip)"
    assert SUMMARY_MARKER_HIDDEN_PREFIX == "[//]: # (ai-pr-review-summary"


def test_build_summary_marker_hidden_with_valid_sha() -> None:
    marker = build_summary_marker(_VALID_SHA, hidden=True)
    assert marker == f"[//]: # (ai-pr-review-summary sha={_VALID_SHA})"


def test_build_summary_marker_hidden_without_sha() -> None:
    marker = build_summary_marker("not-a-sha", hidden=True)
    assert marker == "[//]: # (ai-pr-review-summary)"


def test_has_summary_marker_detects_hidden_form() -> None:
    assert has_summary_marker("[//]: # (ai-pr-review-summary sha=abc1234)") is True
    assert has_summary_marker("[//]: # (ai-pr-review-summary)") is True


def test_extract_summary_sha_hidden_form() -> None:
    body = f"[//]: # (ai-pr-review-summary sha={_VALID_SHA})\n\n## Summary"
    assert extract_summary_sha(body) == _VALID_SHA


def test_extract_summary_sha_round_trips_hidden_build_result() -> None:
    marker = build_summary_marker(_VALID_SHA, hidden=True)
    body = f"{marker}\n\nbody content"
    assert extract_summary_sha(body) == _VALID_SHA


def test_replace_summary_sha_preserves_hidden_format() -> None:
    """The format-preserving branch is the crux of #699: a watermark advance
    against an already-hidden-form comment must not regress it back to a
    visible `<!-- -->` comment."""
    body = "[//]: # (ai-pr-review-summary sha=abc1234)\n\n## Summary body"
    result = replace_summary_sha(body, "def5678")
    assert result == "[//]: # (ai-pr-review-summary sha=def5678)\n\n## Summary body"
    assert "<!--" not in result


def test_replace_summary_sha_preserves_html_comment_format() -> None:
    """Sibling of the above: the HTML-comment branch (GitHub/GitLab, and any
    not-yet-regenerated Bitbucket comment) must not switch to hidden form."""
    body = "<!-- ai-pr-review-summary sha=abc1234 -->\n\n## Summary body"
    result = replace_summary_sha(body, "def5678")
    assert result == "<!-- ai-pr-review-summary sha=def5678 -->\n\n## Summary body"
    assert "[//]" not in result


def test_replace_summary_sha_hidden_marker_without_sha_field_adds_sha() -> None:
    body = "[//]: # (ai-pr-review-summary)\nbody"
    result = replace_summary_sha(body, "def5678")
    assert result == "[//]: # (ai-pr-review-summary sha=def5678)\nbody"


def test_has_inline_marker_detects_hidden_form() -> None:
    assert has_inline_marker(f"comment text\n{INLINE_MARKER_HIDDEN}") is True


def test_has_skip_marker_detects_hidden_form() -> None:
    assert has_skip_marker(f"comment text\n{SKIP_MARKER_HIDDEN}") is True


def test_has_skip_marker_detects_html_comment_form() -> None:
    assert has_skip_marker(f"comment text\n{SKIP_MARKER}") is True


def test_append_inline_marker_hidden_form() -> None:
    result = append_inline_marker("body", marker=INLINE_MARKER_HIDDEN)
    assert INLINE_MARKER_HIDDEN in result
    assert INLINE_MARKER not in result.replace(INLINE_MARKER_HIDDEN, "")


def test_append_inline_marker_hidden_form_is_idempotent() -> None:
    once = append_inline_marker("body", marker=INLINE_MARKER_HIDDEN)
    twice = append_inline_marker(once, marker=INLINE_MARKER_HIDDEN)
    assert once == twice
    assert twice.count(INLINE_MARKER_HIDDEN) == 1


def test_append_inline_marker_cross_form_is_noop() -> None:
    """A body already carrying the old HTML-comment marker must not also
    gain the hidden-form marker — has_inline_marker() gates on either form."""
    body = append_inline_marker("body")  # old HTML-comment form
    result = append_inline_marker(body, marker=INLINE_MARKER_HIDDEN)
    assert result == body
    assert INLINE_MARKER_HIDDEN not in result


def test_append_skip_marker_hidden_forms() -> None:
    result = append_skip_marker(
        "skipped", inline_marker=INLINE_MARKER_HIDDEN, skip_marker=SKIP_MARKER_HIDDEN
    )
    assert INLINE_MARKER_HIDDEN in result
    assert SKIP_MARKER_HIDDEN in result
    assert INLINE_MARKER not in result
    assert SKIP_MARKER not in result


def test_append_skip_marker_hidden_forms_idempotent() -> None:
    once = append_skip_marker(
        "skipped", inline_marker=INLINE_MARKER_HIDDEN, skip_marker=SKIP_MARKER_HIDDEN
    )
    twice = append_skip_marker(
        once, inline_marker=INLINE_MARKER_HIDDEN, skip_marker=SKIP_MARKER_HIDDEN
    )
    assert once == twice
    assert twice.count(INLINE_MARKER_HIDDEN) == 1
    assert twice.count(SKIP_MARKER_HIDDEN) == 1


def test_append_skip_marker_defaults_unchanged() -> None:
    """Default (no kwargs) behavior — used by GitHub/GitLab — is untouched."""
    result = append_skip_marker("skipped")
    assert INLINE_MARKER in result
    assert SKIP_MARKER in result


# ---------------------------------------------------------------------------
# build_id_map_marker / extract_id_map — default (GitHub/GitLab) form
# ---------------------------------------------------------------------------


def test_build_id_map_marker_default_is_html_comment() -> None:
    """hidden=False (the default, unchanged for GitHub/GitLab) still emits
    the raw HTML comment -- this must not regress when hidden=True is added
    for Bitbucket."""
    marker = build_id_map_marker({"a|b.py|1|deadbeef1234": 1})
    assert marker.startswith("<!-- ai-pr-review-id-map: ")
    assert marker.endswith(" -->")
    assert extract_id_map(marker) == {"a|b.py|1|deadbeef1234": 1}


def test_extract_id_map_no_marker_returns_empty() -> None:
    assert extract_id_map("no marker here") == {}


def test_extract_id_map_corrupt_html_comment_returns_empty() -> None:
    assert extract_id_map("<!-- ai-pr-review-id-map: {not json} -->") == {}


# ---------------------------------------------------------------------------
# build_id_map_marker / extract_id_map — hidden form (Bitbucket, #<issue>)
# ---------------------------------------------------------------------------


def test_build_id_map_marker_hidden_is_reference_link_form() -> None:
    marker = build_id_map_marker({"a|b.py|1|deadbeef1234": 1}, hidden=True)
    assert marker.startswith("[//]: # (ai-pr-review-id-map:")
    assert marker.endswith(")")
    # No raw HTML comment syntax at all -- Bitbucket's renderer would show it
    # as literal text (#699).
    assert "<!--" not in marker


def test_id_map_marker_hidden_round_trips() -> None:
    id_map = {"code-reviewer|src/app.py|42|abc123def456": 1, "phpcs|b.py|9|111122223333": 2}
    marker = build_id_map_marker(id_map, hidden=True)
    assert extract_id_map(marker) == id_map


def test_id_map_marker_hidden_survives_a_path_containing_parens() -> None:
    """The whole reason for base64-encoding the hidden form: a fingerprint
    embeds the finding's file path verbatim, and a path containing `)` would
    otherwise prematurely close the `[//]: # (...)` reference-link
    definition, corrupting the marker and leaking JSON into the visible
    comment."""
    id_map = {"code-reviewer|src/utils (copy).py|3|abc123def456": 7}
    marker = build_id_map_marker(id_map, hidden=True)
    # Exactly one closing paren -- the one that closes the link definition
    # itself, not one smuggled in from the payload.
    assert marker.count(")") == 1
    assert extract_id_map(marker) == id_map


def test_extract_id_map_hidden_corrupt_base64_returns_empty() -> None:
    assert extract_id_map("[//]: # (ai-pr-review-id-map:not-valid-base64!!!)") == {}


def test_extract_id_map_prefers_html_comment_over_hidden_form() -> None:
    """If a body somehow carries both forms (shouldn't happen in practice,
    since a given provider always emits exactly one), the plain HTML-comment
    form takes priority -- matching the order the two regexes are tried."""
    html_map = {"x|y.py|1|aaaaaaaaaaaa": 1}
    hidden_map = {"x|y.py|1|bbbbbbbbbbbb": 2}
    body = (
        build_id_map_marker(html_map)
        + "\n"
        + build_id_map_marker(hidden_map, hidden=True)
    )
    assert extract_id_map(body) == html_map


# ---------------------------------------------------------------------------
# build_verdicts_marker / extract_verdicts / upsert_verdicts_marker
# ---------------------------------------------------------------------------


def test_build_verdicts_marker_round_trips() -> None:
    verdicts = {"a|b.py|1|abc123def456": "dismissed", "c|d.py|2|def456abc123": "fixed"}
    marker = build_verdicts_marker(verdicts)
    assert marker.startswith("<!-- ai-pr-review-verdicts: ")
    assert marker.endswith(" -->")
    assert extract_verdicts(marker) == verdicts


def test_extract_verdicts_no_marker_returns_empty() -> None:
    assert extract_verdicts("no marker here") == {}


def test_extract_verdicts_corrupt_json_returns_empty() -> None:
    assert extract_verdicts("<!-- ai-pr-review-verdicts: {not json} -->") == {}


def test_extract_verdicts_drops_unknown_verdict_values() -> None:
    """A future verdict type this version doesn't recognize is dropped
    individually rather than discarding the whole map."""
    body = '<!-- ai-pr-review-verdicts: {"a":"dismissed","b":"some-future-verdict"} -->'
    assert extract_verdicts(body) == {"a": "dismissed"}


def test_upsert_verdicts_marker_appends_when_absent() -> None:
    body = "some review body\n"
    result = upsert_verdicts_marker(body, {"a|b.py|1|abc123def456": "dismissed"})
    assert result.startswith(body)
    assert extract_verdicts(result) == {"a|b.py|1|abc123def456": "dismissed"}


def test_upsert_verdicts_marker_appends_with_separator_when_no_trailing_newline() -> None:
    body = "some review body"
    result = upsert_verdicts_marker(body, {"a|b.py|1|abc123def456": "dismissed"})
    assert result.startswith("some review body\n<!-- ai-pr-review-verdicts:")


def test_upsert_verdicts_marker_replaces_existing_marker_in_place() -> None:
    body = "some review body\n" + build_verdicts_marker({"a|b.py|1|abc123def456": "dismissed"})
    updated = upsert_verdicts_marker(
        body, {"a|b.py|1|abc123def456": "dismissed", "c|d.py|2|def456abc123": "fixed"}
    )
    # Exactly one marker, not two -- a naive append would duplicate it.
    assert updated.count("ai-pr-review-verdicts:") == 1
    assert extract_verdicts(updated) == {
        "a|b.py|1|abc123def456": "dismissed",
        "c|d.py|2|def456abc123": "fixed",
    }
    assert updated.startswith("some review body\n")
