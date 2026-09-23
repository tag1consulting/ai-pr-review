"""Tests for ai_pr_review.vcs.marker."""

from __future__ import annotations

import pytest

from ai_pr_review.vcs.marker import (
    ACKS_MARKER_HIDDEN_PREFIX,
    INLINE_MARKER,
    INLINE_MARKER_HIDDEN,
    SKIP_MARKER,
    SKIP_MARKER_HIDDEN,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SUMMARY_MARKER_PREFIX,
    InlineMeta,
    append_inline_marker,
    append_skip_marker,
    build_acks_marker,
    build_id_map_marker,
    build_inline_meta_marker,
    build_judge_map_marker,
    build_summary_marker,
    build_verdicts_marker,
    extract_acks,
    extract_id_map,
    extract_inline_meta,
    extract_judge_map,
    extract_summary_sha,
    extract_verdicts,
    has_inline_marker,
    has_skip_marker,
    has_summary_marker,
    replace_summary_sha,
    upsert_acks_marker,
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


# ---------------------------------------------------------------------------
# build_verdicts_marker / extract_verdicts / upsert_verdicts_marker -- hidden
# form (Bitbucket, #839 follow-up)
# ---------------------------------------------------------------------------


def test_build_verdicts_marker_default_is_html_comment() -> None:
    """hidden=False (the default, unchanged for GitHub) still emits the raw
    HTML comment -- this must not regress when hidden=True is added for
    Bitbucket."""
    marker = build_verdicts_marker({"a|b.py|1|abc123def456": "dismissed"})
    assert marker.startswith("<!-- ai-pr-review-verdicts: ")
    assert marker.endswith(" -->")
    assert extract_verdicts(marker) == {"a|b.py|1|abc123def456": "dismissed"}


def test_build_verdicts_marker_hidden_is_reference_link_form() -> None:
    marker = build_verdicts_marker({"a|b.py|1|abc123def456": "dismissed"}, hidden=True)
    assert marker.startswith("[//]: # (ai-pr-review-verdicts:")
    assert marker.endswith(")")
    # No raw HTML comment syntax at all -- Bitbucket's renderer would show it
    # as literal text (#699).
    assert "<!--" not in marker


def test_verdicts_marker_hidden_round_trips() -> None:
    verdicts = {"a|b.py|1|abc123def456": "dismissed", "c|d.py|2|def456abc123": "fixed"}
    marker = build_verdicts_marker(verdicts, hidden=True)
    assert extract_verdicts(marker) == verdicts


def test_verdicts_marker_hidden_survives_a_path_containing_parens() -> None:
    """The whole reason for base64-encoding the hidden form: a fingerprint
    embeds the finding's file path verbatim, and a path containing `)` would
    otherwise prematurely close the `[//]: # (...)` reference-link
    definition, corrupting the marker and leaking JSON into the visible
    comment."""
    verdicts = {"code-reviewer|src/utils (copy).py|3|abc123def456": "dismissed"}
    marker = build_verdicts_marker(verdicts, hidden=True)
    assert marker.count(")") == 1
    assert extract_verdicts(marker) == verdicts


def test_extract_verdicts_hidden_corrupt_base64_returns_empty() -> None:
    # "not-valid-base64!!!" contains characters (`-`, `!`) outside the hidden
    # marker regex's own capture class ([A-Za-z0-9+/=]+), so that string
    # would never even match the marker pattern -- exercising the "no
    # candidates found" path, not the base64.b64decode exception handler
    # this test is named for. "A" alone matches the regex's charset but is
    # invalid base64 (a data length of 1 is never valid), so it actually
    # reaches and exercises the except branch.
    assert extract_verdicts("[//]: # (ai-pr-review-verdicts:A)") == {}


def test_extract_verdicts_hidden_valid_base64_invalid_utf8_returns_empty() -> None:
    # base64 of b"\xff\xfe" decodes successfully as base64 but is not valid
    # UTF-8 -- exercises the UnicodeDecodeError branch specifically, distinct
    # from the ValueError (bad base64) branch covered above.
    assert extract_verdicts("[//]: # (ai-pr-review-verdicts://4=)") == {}


def test_build_verdicts_marker_hidden_round_trips_empty_dict() -> None:
    marker = build_verdicts_marker({}, hidden=True)
    assert extract_verdicts(marker) == {}


def test_extract_verdicts_hidden_ignores_a_coexisting_hidden_id_map_marker() -> None:
    """Bitbucket's single summary comment carries both a hidden id-map
    marker and a hidden verdicts marker at once -- the real production
    shape, per this file's own Bitbucket-parity note. Both share the same
    `[//]: # (ai-pr-review-<name>:<base64>)` structure. Confirm the literal
    prefix keeps them from cross-matching each other's payload."""
    id_map_marker = build_id_map_marker({"F1": 12345}, hidden=True)
    verdicts_marker = build_verdicts_marker(
        {"a|b.py|1|abc123def456": "dismissed"}, hidden=True
    )
    body = f"some review body\n{id_map_marker}\n{verdicts_marker}"
    assert extract_verdicts(body) == {"a|b.py|1|abc123def456": "dismissed"}
    assert extract_id_map(body) == {"F1": 12345}


def test_extract_verdicts_later_form_wins_by_position_not_by_form() -> None:
    """Unlike extract_id_map (which always prefers the HTML-comment form
    over the hidden form regardless of position), extract_verdicts follows
    extract_inline_meta's position-based rule: whichever form appears LAST
    in the body wins, matching the "a forged marker-shaped string earlier in
    the body cannot appear after the real one" invariant. Checked both
    orderings so this isn't a coincidence of which regex happens to run
    first."""
    html_verdicts = {"x|y.py|1|aaaaaaaaaaaa": "dismissed"}
    hidden_verdicts = {"x|y.py|1|bbbbbbbbbbbb": "fixed"}

    hidden_last = (
        build_verdicts_marker(html_verdicts) + "\n" + build_verdicts_marker(hidden_verdicts, hidden=True)
    )
    assert extract_verdicts(hidden_last) == hidden_verdicts

    html_last = (
        build_verdicts_marker(hidden_verdicts, hidden=True) + "\n" + build_verdicts_marker(html_verdicts)
    )
    assert extract_verdicts(html_last) == html_verdicts


def test_upsert_verdicts_marker_hidden_appends_when_absent() -> None:
    body = "some review body\n"
    result = upsert_verdicts_marker(
        body, {"a|b.py|1|abc123def456": "dismissed"}, hidden=True
    )
    assert result.startswith(body)
    assert "<!--" not in result
    assert extract_verdicts(result) == {"a|b.py|1|abc123def456": "dismissed"}


def test_upsert_verdicts_marker_hidden_appends_with_blank_line_when_no_trailing_newline() -> None:
    """A `[//]: # (...)` reference-link definition appended after a single
    newline becomes a CommonMark lazy-continuation line and renders as
    literal text instead of being hidden (#699) -- it needs a full blank
    line before it, unlike the HTML-comment form. Must route through
    `_hidden_marker_separator`, the same helper `append_inline_marker` uses
    for the same reason, not the plain single-newline-or-empty logic the
    default form uses."""
    body = "some review body"
    result = upsert_verdicts_marker(
        body, {"a|b.py|1|abc123def456": "dismissed"}, hidden=True
    )
    assert result.startswith("some review body\n\n[//]: # (ai-pr-review-verdicts:")


def test_upsert_verdicts_marker_hidden_replaces_existing_hidden_marker_in_place() -> None:
    body = "some review body\n" + build_verdicts_marker(
        {"a|b.py|1|abc123def456": "dismissed"}, hidden=True
    )
    updated = upsert_verdicts_marker(
        body,
        {"a|b.py|1|abc123def456": "dismissed", "c|d.py|2|def456abc123": "fixed"},
        hidden=True,
    )
    assert updated.count("ai-pr-review-verdicts:") == 1
    assert extract_verdicts(updated) == {
        "a|b.py|1|abc123def456": "dismissed",
        "c|d.py|2|def456abc123": "fixed",
    }


def test_upsert_verdicts_marker_hidden_replaces_a_pre_existing_html_form_marker() -> None:
    """A body that already carries the default HTML-comment form (e.g. from
    before a repo's provider config changed) gets that marker replaced, not
    duplicated, even when the caller now asks for the hidden form."""
    body = "some review body\n" + build_verdicts_marker({"a|b.py|1|abc123def456": "dismissed"})
    updated = upsert_verdicts_marker(
        body, {"a|b.py|1|abc123def456": "fixed"}, hidden=True
    )
    assert updated.count("ai-pr-review-verdicts:") == 1
    assert "<!--" not in updated
    assert extract_verdicts(updated) == {"a|b.py|1|abc123def456": "fixed"}


def test_upsert_verdicts_marker_replaces_last_by_position_when_both_forms_present() -> None:
    """If a body somehow carries both forms, the one replaced must be
    whichever occurs LAST by position -- matching extract_verdicts' own
    "last one wins" rule for deciding which marker is live. Replacing
    whichever form happens to be checked first (an
    `A.search(body) or B.search(body)` chain) would patch a stale, shadowed
    marker while extract_verdicts kept reading the untouched one that sorts
    later, so the write would appear to succeed while the verdicts it wrote
    are silently orphaned. Here the HTML form sorts first and the hidden
    form sorts last, so the hidden one must be the one replaced."""
    body = (
        "some review body\n"
        + build_verdicts_marker({"a|b.py|1|abc123def456": "dismissed"})
        + "\n"
        + build_verdicts_marker({"c|d.py|2|def456abc123": "fixed"}, hidden=True)
    )
    updated = upsert_verdicts_marker(
        body, {"c|d.py|2|def456abc123": "recurred"}, hidden=True
    )
    assert updated.count("ai-pr-review-verdicts:") == 2
    # The last-by-position marker (originally hidden) now carries the new
    # payload. The earlier HTML-form marker is untouched.
    assert extract_verdicts(updated) == {"c|d.py|2|def456abc123": "recurred"}
    assert '"a|b.py|1|abc123def456":"dismissed"' in updated


def test_upsert_verdicts_marker_replaces_last_match_of_the_same_form() -> None:
    """#886 hardening regression: when a body carries TWO markers of the
    SAME form (e.g. a forged plain marker earlier, from unsanitized
    LLM-derived text, followed by the real one), upsert must patch the LAST
    one -- matching extract_verdicts' own last-by-position rule. Before this
    fix, upsert used `.search()` per form (first match only), so it silently
    rewrote the forged earlier marker while the caller's new verdicts were
    orphaned behind the still-untouched real one, which extract_verdicts
    kept reading."""
    body = (
        build_verdicts_marker({"forged|fp|1|aaaaaaaaaaaa": "dismissed"})
        + "\ntext\n"
        + build_verdicts_marker({"real|fp|1|bbbbbbbbbbbb": "fixed"})
    )
    updated = upsert_verdicts_marker(
        body, {"real|fp|1|bbbbbbbbbbbb": "fixed", "new|fp|2|cccccccccccc": "dismissed"}
    )
    assert extract_verdicts(updated) == {
        "real|fp|1|bbbbbbbbbbbb": "fixed",
        "new|fp|2|cccccccccccc": "dismissed",
    }
    # The forged earlier marker is untouched, not merged into the result.
    assert '"forged|fp|1|aaaaaaaaaaaa":"dismissed"' in updated


# ---------------------------------------------------------------------------
# build_acks_marker / extract_acks / upsert_acks_marker (#874, Bitbucket only)
# ---------------------------------------------------------------------------


def test_build_acks_marker_is_hidden_form() -> None:
    marker = build_acks_marker([1, 2, 3])
    assert marker.startswith(ACKS_MARKER_HIDDEN_PREFIX)
    assert "<!--" not in marker


def test_extract_acks_round_trips() -> None:
    marker = build_acks_marker([5, 3, 1, 4])
    assert extract_acks(marker) == frozenset({1, 3, 4, 5})


def test_extract_acks_no_marker_returns_empty() -> None:
    assert extract_acks("some review body\n") == frozenset()


def test_extract_acks_corrupt_base64_returns_empty() -> None:
    # "A" matches the marker regex's character class but is invalid base64
    # (a data length of 1 is never valid) -- same reasoning as
    # test_extract_verdicts_hidden_corrupt_base64_returns_empty above.
    assert extract_acks("[//]: # (ai-pr-review-acks:A)") == frozenset()


def test_extract_acks_valid_json_wrong_shape_returns_empty() -> None:
    import base64
    import json

    payload = base64.b64encode(json.dumps({"not": "a list"}).encode()).decode()
    assert extract_acks(f"[//]: # (ai-pr-review-acks:{payload})") == frozenset()


def test_build_acks_marker_caps_at_max_and_keeps_highest_ids() -> None:
    from ai_pr_review.vcs.marker import _MAX_ACKED_IDS

    ids = list(range(_MAX_ACKED_IDS + 50))
    marker = build_acks_marker(ids)
    acked = extract_acks(marker)
    assert len(acked) == _MAX_ACKED_IDS
    # Oldest (smallest) ids are the ones dropped -- comment ids are
    # monotonically increasing, so "oldest" and "smallest" are the same
    # ordering.
    assert min(acked) == 50
    assert max(acked) == _MAX_ACKED_IDS + 49


def test_upsert_acks_marker_appends_when_absent() -> None:
    body = "some review body\n"
    result = upsert_acks_marker(body, [1, 2])
    assert result.startswith(body)
    assert extract_acks(result) == frozenset({1, 2})


def test_upsert_acks_marker_replaces_existing_marker_in_place() -> None:
    body = "some review body\n" + build_acks_marker([1, 2])
    updated = upsert_acks_marker(body, [1, 2, 3])
    assert updated.count("ai-pr-review-acks:") == 1
    assert extract_acks(updated) == frozenset({1, 2, 3})


def test_upsert_acks_marker_uses_blank_line_separator_when_no_trailing_newline() -> None:
    body = "some review body"
    result = upsert_acks_marker(body, [1])
    assert result.startswith("some review body\n\n[//]: # (ai-pr-review-acks:")


# ---------------------------------------------------------------------------
# InlineMeta / build_inline_meta_marker / extract_inline_meta
# ---------------------------------------------------------------------------


def test_build_inline_meta_marker_round_trips() -> None:
    marker = build_inline_meta_marker(
        fingerprint="code-reviewer|app.py|10|abc123def456", category="secret", severity="High"
    )
    assert marker.startswith("<!-- ai-pr-review-finding:")
    assert marker.endswith(" -->")
    meta = extract_inline_meta(marker)
    assert meta == InlineMeta(
        fp="code-reviewer|app.py|10|abc123def456", cat="secret", sev="High"
    )


def test_build_inline_meta_marker_round_trips_judge_data() -> None:
    """Judge-verdict instrumentation: judge_verdict/corroborated/confidence
    round-trip through the marker when supplied."""
    marker = build_inline_meta_marker(
        fingerprint="code-reviewer|app.py|10|abc123def456",
        category="secret",
        severity="High",
        judge_verdict="downrank",
        corroborated=True,
        confidence=63,
    )
    meta = extract_inline_meta(marker)
    assert meta == InlineMeta(
        fp="code-reviewer|app.py|10|abc123def456",
        cat="secret",
        sev="High",
        judge_verdict="downrank",
        corroborated=True,
        confidence=63,
    )


def test_build_inline_meta_marker_omits_judge_data_when_absent() -> None:
    """A marker built with no judge data at all (judge pass never ran, or a
    caller predating this feature) has byte-for-byte the same shape as
    before this feature existed -- no `jv`/`corr`/`conf` keys at all."""
    marker = build_inline_meta_marker(
        fingerprint="code-reviewer|app.py|10|abc123def456", category="secret", severity="High"
    )
    assert '"jv"' not in marker
    assert '"corr"' not in marker
    assert '"conf"' not in marker
    meta = extract_inline_meta(marker)
    assert meta is not None
    assert meta.judge_verdict is None
    assert meta.corroborated is False
    assert meta.confidence is None


def test_extract_inline_meta_unknown_judge_verdict_drops_to_none() -> None:
    """A future judge_verdict value this version doesn't recognize degrades
    to None rather than being trusted verbatim."""
    import base64
    import json

    payload = json.dumps({"fp": "src|f.py|1|abc", "jv": "not-a-real-verdict"})
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    meta = extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->")
    assert meta is not None
    assert meta.judge_verdict is None


def test_extract_inline_meta_out_of_range_confidence_drops_to_none() -> None:
    """A confidence value outside 0-100 (corrupt or forged payload) degrades
    to None rather than being trusted verbatim."""
    import base64
    import json

    payload = json.dumps({"fp": "src|f.py|1|abc", "conf": 999})
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    meta = extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->")
    assert meta is not None
    assert meta.confidence is None


def test_extract_inline_meta_boolean_confidence_drops_to_none() -> None:
    """A JSON boolean for `conf` (true/false) must not be trusted as 1/0 —
    bool is a subclass of int in Python, so this needs an explicit guard
    beyond isinstance(conf_raw, int)."""
    import base64
    import json

    for bool_value in (True, False):
        payload = json.dumps({"fp": "src|f.py|1|abc", "conf": bool_value})
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        meta = extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->")
        assert meta is not None
        assert meta.confidence is None


def test_extract_inline_meta_no_marker_returns_none() -> None:
    assert extract_inline_meta("just a plain comment body") == extract_inline_meta("")
    assert extract_inline_meta("just a plain comment body") is None


def test_extract_inline_meta_undecodable_base64_returns_none() -> None:
    # Not valid base64 (contains a character outside the marker's charset,
    # so the regex itself won't match) -- verifies the "no marker" path is
    # not accidentally triggered by malformed-but-marker-shaped text either.
    assert extract_inline_meta("<!-- ai-pr-review-finding:not_valid_base64! -->") is None


def test_extract_inline_meta_valid_base64_invalid_json_returns_none() -> None:
    import base64

    encoded = base64.b64encode(b"not json at all").decode("ascii")
    assert extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->") is None


def test_extract_inline_meta_non_dict_payload_returns_none() -> None:
    import base64

    encoded = base64.b64encode(b'["a", "list", "not", "a", "dict"]').decode("ascii")
    assert extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->") is None


def test_extract_inline_meta_missing_fp_returns_none() -> None:
    import base64
    import json

    payload = json.dumps({"cat": "secret", "sev": "High"})
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    assert extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->") is None


def test_extract_inline_meta_empty_fp_returns_none() -> None:
    import base64
    import json

    payload = json.dumps({"fp": "", "cat": "secret", "sev": "High"})
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    assert extract_inline_meta(f"<!-- ai-pr-review-finding:{encoded} -->") is None


def test_extract_inline_meta_unknown_category_drops_to_none_keeps_fp() -> None:
    """A future category this version doesn't recognize degrades to None
    (wildcard-compatible for categories_compatible) without discarding fp."""
    marker = build_inline_meta_marker(
        fingerprint="src|f.py|1|abc", category="not-a-real-category", severity="High"
    )
    meta = extract_inline_meta(marker)
    assert meta is not None
    assert meta.fp == "src|f.py|1|abc"
    assert meta.cat is None
    assert meta.sev == "High"


def test_extract_inline_meta_unknown_severity_drops_to_none_keeps_fp() -> None:
    marker = build_inline_meta_marker(
        fingerprint="src|f.py|1|abc", category="secret", severity="Ludicrous"
    )
    meta = extract_inline_meta(marker)
    assert meta is not None
    assert meta.fp == "src|f.py|1|abc"
    assert meta.cat == "secret"
    assert meta.sev is None


def test_extract_inline_meta_last_match_wins_over_forged_earlier_marker() -> None:
    """Adversarial case documented in marker.py's module comment: a hostile
    finding's suggested code (rendered unescaped ahead of the real marker,
    per is_suggestion_safe's triple-backtick-only rejection) could contain
    text shaped like a real marker. extract_inline_meta must always trust
    the LAST match in the body -- the renderer always appends the real
    marker after everything else -- not the first."""
    real_fp = "code-reviewer|app.py|10|realreal1234"
    forged_fp = "attacker|evil.py|999|forgedforged"
    forged_marker = build_inline_meta_marker(
        fingerprint=forged_fp, category="injection", severity="Critical"
    )
    real_marker = build_inline_meta_marker(
        fingerprint=real_fp, category="secret", severity="High"
    )
    body = (
        "🔴 **[High]** [code-reviewer] leaked key\n"
        "```suggestion\n"
        f"{forged_marker}\n"
        "```\n"
        "<!-- ai-pr-review-inline -->\n"
        f"{real_marker}"
    )
    meta = extract_inline_meta(body)
    assert meta is not None
    assert meta.fp == real_fp
    assert meta.cat == "secret"
    assert meta.sev == "High"


def test_judge_map_marker_round_trips() -> None:
    judge_map = {
        "code-reviewer|app.py|10|abc123": {"jv": "downrank", "conf": 60},
        "security-reviewer|b.py|5|def456": {"jv": "keep", "corr": True},
    }
    marker = build_judge_map_marker(judge_map)
    body = f"some review body\n{marker}"
    result = extract_judge_map(body)
    assert result == {
        "code-reviewer|app.py|10|abc123": {"jv": "downrank", "corr": False, "conf": 60},
        "security-reviewer|b.py|5|def456": {"jv": "keep", "corr": True, "conf": None},
    }


def test_extract_judge_map_no_marker_returns_empty() -> None:
    assert extract_judge_map("no marker here") == {}


def test_extract_judge_map_malformed_json_returns_empty() -> None:
    body = "<!-- ai-pr-review-judge-map: {not valid json} -->"
    assert extract_judge_map(body) == {}


def test_extract_judge_map_drops_entry_with_invalid_verdict() -> None:
    import json

    payload = json.dumps({"fp1": {"jv": "maybe"}, "fp2": {"jv": "keep"}})
    body = f"<!-- ai-pr-review-judge-map: {payload} -->"
    assert extract_judge_map(body) == {"fp2": {"jv": "keep", "corr": False, "conf": None}}


def test_extract_judge_map_boolean_confidence_drops_to_none() -> None:
    import json

    payload = json.dumps({"fp1": {"jv": "keep", "conf": True}})
    body = f"<!-- ai-pr-review-judge-map: {payload} -->"
    assert extract_judge_map(body) == {"fp1": {"jv": "keep", "corr": False, "conf": None}}
