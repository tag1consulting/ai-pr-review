"""Tests for the shared PR context block builder (#813)."""

from __future__ import annotations

from ai_pr_review.review.pr_context import (
    DEFAULT_MAX_BODY_CHARS,
    build_shared_context_block,
)


def test_includes_title_body_and_manifest() -> None:
    block = build_shared_context_block(
        manifest_text="src/foo.py\nsrc/bar.py",
        pr_title="Add widget",
        pr_body="This fixes #1 by adding a widget.",
    )
    assert block.startswith("<pr-context>\n")
    assert block.endswith("\n</pr-context>")
    assert "## PR Title\n\nAdd widget" in block
    assert "## PR Description\n\nThis fixes #1 by adding a widget." in block
    assert "## Changed Files\n\nsrc/foo.py\nsrc/bar.py" in block


def test_empty_everything_returns_empty_string() -> None:
    assert build_shared_context_block(manifest_text="", pr_title="", pr_body="") == ""


def test_manifest_only_when_title_and_body_blank() -> None:
    block = build_shared_context_block(
        manifest_text="src/foo.py", pr_title="", pr_body=""
    )
    assert "## Changed Files\n\nsrc/foo.py" in block
    assert "## PR Title" not in block
    assert "## PR Description" not in block


def test_strips_html_comments_from_body() -> None:
    block = build_shared_context_block(
        manifest_text="",
        pr_title="Add widget",
        pr_body="<!-- template boilerplate -->Real content here.",
    )
    assert "template boilerplate" not in block
    assert "Real content here." in block


def test_strips_multiline_html_comment() -> None:
    body = "Before.\n<!--\nmulti\nline\ncomment\n-->\nAfter."
    block = build_shared_context_block(manifest_text="", pr_title="", pr_body=body)
    assert "multi" not in block
    assert "Before." in block
    assert "After." in block


def test_body_entirely_html_comment_yields_no_description_section() -> None:
    block = build_shared_context_block(
        manifest_text="",
        pr_title="Add widget",
        pr_body="<!-- nothing but a comment -->",
    )
    assert "## PR Description" not in block
    assert "## PR Title" in block


def test_truncates_long_body() -> None:
    long_body = "x" * (DEFAULT_MAX_BODY_CHARS + 500)
    block = build_shared_context_block(
        manifest_text="", pr_title="", pr_body=long_body
    )
    assert "_[description truncated]_" in block
    # Kept content should be capped at max_body_chars, not the full input.
    kept = block.split("## PR Description\n\n", 1)[1]
    assert len(kept) < len(long_body)


def test_short_body_not_truncated() -> None:
    block = build_shared_context_block(
        manifest_text="", pr_title="", pr_body="short description"
    )
    assert "_[description truncated]_" not in block


def test_custom_max_body_chars() -> None:
    block = build_shared_context_block(
        manifest_text="",
        pr_title="",
        pr_body="0123456789",
        max_body_chars=5,
    )
    assert "01234" in block
    assert "56789" not in block
    assert "_[description truncated]_" in block
