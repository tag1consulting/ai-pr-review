"""Shared GraphQL review-thread first-comment accessors — issue #822.

`github.py`'s stale-cleanup/review-dismissal paths (`resolve_stale`'s marker
check, `_dismiss_stale_reviews`) and `slash/dismiss.py`'s F-ID
classification/auto-approve paths (`_dismiss_if_all_resolved`,
`_approve_if_pr_fully_resolved`, and friends) each independently
re-implemented an identical accessor set for "read the first comment off a
GitHub GraphQL `reviewThreads` node" — github.py's own module-level
`_first_comment_body`/`_first_comment_author_login`/`_first_comment_review_id`,
and dismiss.py's `_first_comment`/`_first_comment_body`/
`_first_comment_author_login`/`_thread_review_id`/`_first_comment_id`. Both
operate on the exact same shape (`thread["comments"]["nodes"][0]`) returned
by `GitHubProvider.fetch_review_threads()`'s GraphQL query, so this module is
the single source of truth for it.

`count_unresolved_owned_threads` factors out the "map review id -> count of
currently-unresolved threads we own" loop that `github.py`'s
`_dismiss_stale_reviews` and `slash/dismiss.py`'s `_approve_if_pr_fully_resolved`
(and, filtered to one review id, `_dismiss_if_all_resolved`) each
independently built via a near-identical `for t in threads: ...` loop. The
one deliberate difference between call sites — `bot_login=None` in
dismiss.py's two call sites vs. a normalized `graphql_bot_login(...)` in
github.py's — is preserved as a caller-supplied parameter, not flattened
away; see `dismiss._dismiss_if_all_resolved`'s docstring for why that
difference is intentional, not a bug.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ai_pr_review.vcs._stale import is_owned_by_us


def first_comment(thread: dict[str, Any]) -> dict[str, Any]:
    """Return the first comment dict of a GraphQL `reviewThreads` node, or `{}`."""
    nodes = ((thread.get("comments") or {}).get("nodes")) or []
    return nodes[0] if nodes else {}


def first_comment_body(thread: dict[str, Any]) -> str:
    return first_comment(thread).get("body") or ""


def first_comment_author_login(thread: dict[str, Any]) -> str:
    author = first_comment(thread).get("author") or {}
    return author.get("login") or ""


def first_comment_review_id(thread: dict[str, Any]) -> int | None:
    review = first_comment(thread).get("pullRequestReview") or {}
    rid = review.get("databaseId")
    return int(rid) if isinstance(rid, int) else None


def first_comment_id(thread: dict[str, Any]) -> int | None:
    cid = first_comment(thread).get("databaseId")
    return int(cid) if isinstance(cid, int) else None


def count_unresolved_owned_threads(
    threads: Sequence[dict[str, Any]],
    *,
    bot_login: str | None,
) -> dict[int, int]:
    """Map review id -> count of currently-unresolved threads we own.

    A thread counts iff it is not yet resolved, its first comment passes
    `is_owned_by_us(..., kind="inline")` against `bot_login` (see this
    module's docstring for why callers pass different values here), and its
    first comment carries a `pullRequestReview.databaseId` (threads without
    one -- e.g. a legacy/orphaned comment -- are excluded from every count,
    matching every pre-extraction copy of this loop).
    """
    counts: dict[int, int] = {}
    for t in threads:
        if t.get("isResolved"):
            continue
        body = first_comment_body(t)
        author = first_comment_author_login(t) or None
        if not is_owned_by_us(body, author, bot_login, kind="inline"):
            continue
        rid = first_comment_review_id(t)
        if rid is None:
            continue
        counts[rid] = counts.get(rid, 0) + 1
    return counts
