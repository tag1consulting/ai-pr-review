"""Tests for ai_pr_review.vcs._stale.is_owned_by_us."""

from __future__ import annotations

from ai_pr_review.vcs._stale import is_owned_by_us
from ai_pr_review.vcs.marker import INLINE_MARKER, SUMMARY_MARKER_PREFIX


def test_owned_by_us_inline_with_marker_and_matching_author() -> None:
    body = f"finding text\n{INLINE_MARKER}"
    assert is_owned_by_us(body, "github-actions[bot]", "github-actions[bot]") is True


def test_not_owned_when_no_inline_marker() -> None:
    assert (
        is_owned_by_us(
            "no marker here", "github-actions[bot]", "github-actions[bot]"
        )
        is False
    )


def test_not_owned_when_author_mismatch() -> None:
    body = f"x\n{INLINE_MARKER}"
    assert is_owned_by_us(body, "renovate[bot]", "github-actions[bot]") is False


def test_owned_when_bot_login_none_skips_author_check() -> None:
    body = f"x\n{INLINE_MARKER}"
    # Bitbucket-style: no author info → trust marker alone
    assert is_owned_by_us(body, None, None) is True


def test_owned_when_author_none_and_bot_login_set() -> None:
    body = f"x\n{INLINE_MARKER}"
    # author info missing but we know our bot login → trust the marker
    assert is_owned_by_us(body, None, "github-actions[bot]") is True


def test_summary_kind_uses_summary_marker() -> None:
    body = f"{SUMMARY_MARKER_PREFIX} sha=abc1234 -->\nbody"
    assert is_owned_by_us(body, "x", "x", kind="summary") is True
    # inline marker absence under summary kind: doesn't matter — summary marker is what counts
    assert is_owned_by_us("plain text", "x", "x", kind="summary") is False
