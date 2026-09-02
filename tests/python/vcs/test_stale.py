"""Tests for ai_pr_review.vcs._stale.is_owned_by_us and graphql_bot_login."""

from __future__ import annotations

from ai_pr_review.vcs._stale import graphql_bot_login, is_owned_by_us
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


def test_graphql_bot_login_strips_bot_suffix() -> None:
    assert graphql_bot_login("github-actions[bot]") == "github-actions"


def test_graphql_bot_login_no_suffix_is_unchanged() -> None:
    assert graphql_bot_login("github-actions") == "github-actions"


def test_graphql_bot_login_normalized_value_matches_real_graphql_author() -> None:
    """Issue #717: GitHub's GraphQL API reports the bot's login without the
    REST-style "[bot]" suffix (verified live against a real thread).
    Normalizing before comparison, not passing bot_login=None, is what
    GitHubProvider's resolve_stale/_dismiss_stale_reviews/post_findings
    call sites do -- this keeps the author check as a real defense-in-depth
    signal instead of disabling it entirely."""
    body = f"x\n{INLINE_MARKER}"
    real_graphql_author = "github-actions"
    normalized = graphql_bot_login("github-actions[bot]")
    assert is_owned_by_us(body, real_graphql_author, normalized) is True
    # A different bot's (also suffix-less) GraphQL login must still fail.
    assert is_owned_by_us(body, "renovate", normalized) is False
