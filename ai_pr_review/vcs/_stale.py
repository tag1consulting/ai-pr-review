"""Provider-agnostic marker-gated stale predicate.

All providers (GitHub, GitLab, Bitbucket) share the same ownership rule:
a comment/thread is "ours" iff the body contains INLINE_MARKER (or
SUMMARY_MARKER_PREFIX for summary cleanup) AND, where author info is
available, the author login matches our bot identity.

The actual API calls (resolveReviewThread, DELETE comment, dismiss review)
differ per provider; this module just answers "should we touch this?".
"""

from __future__ import annotations

from typing import Literal

from ai_pr_review.vcs.marker import has_inline_marker, has_summary_marker

MarkerKind = Literal["inline", "summary"]


def is_owned_by_us(
    body: str,
    author_login: str | None,
    bot_login: str | None,
    *,
    kind: MarkerKind = "inline",
) -> bool:
    """Return True iff this comment is safe to resolve/delete as ours.

    Hard preconditions:
    - body must carry the appropriate marker (inline or summary)

    Defense-in-depth (when author_login + bot_login both supplied):
    - author_login must match bot_login

    `bot_login=None` skips the author check (used for providers that don't
    surface author info in a uniform way, e.g., Bitbucket's older comment APIs).
    """
    if kind == "inline":
        if not has_inline_marker(body):
            return False
    else:
        if not has_summary_marker(body):
            return False
    return not (bot_login and author_login and author_login != bot_login)
