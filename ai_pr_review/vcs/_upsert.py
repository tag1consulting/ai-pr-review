"""Shared find-or-create-then-update ("upsert") core for the summary-comment
and skip-comment CRUD paths, plus the sha-watermark in-place patch, across
all three VCS providers (github.py/gitlab.py/bitbucket.py) — issue #822.

Each provider's `post_summary`/`post_skip_comment` do the exact same three
things: list existing marker-bearing comments/notes, PATCH/PUT the oldest
match (index 0) while deleting any duplicates, or POST a fresh one if none
exist. `advance_sha_watermark` does a simpler variant of the same "list, pick
index 0, patch in place" shape, skipping the write entirely when the sha
field hasn't actually changed. `upsert_comment`/`advance_sha_marker` factor
out that control flow, error-message wording (verified byte-identical across
all three providers before extraction), and the `SummaryResult` construction.

Deliberately NOT folded in here: marker/body/footer construction (github's
footer says "AI Review Summary", gitlab/bitbucket's says just "AI Review";
bitbucket uses hidden markers and a lower truncation limit; gitlab has its
own truncation limit), the HTTP verb (github's issue-comments PATCH vs
gitlab/bitbucket's PUT), the payload shape (`{"body": ...}` vs bitbucket's
`{"content": {"raw": ...}}`), and how to read a listed item's current body
back (`item.get("body")` vs bitbucket's `(item.get("content") or
{}).get("raw")`). These are genuine per-provider differences, not
duplication, so they stay as small callables/kwargs each call site supplies
rather than being flattened into one lossy shared body-builder.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ai_pr_review.vcs.marker import replace_summary_sha
from ai_pr_review.vcs.protocol import SummaryResult


def upsert_comment(
    *,
    list_existing: Callable[[], Sequence[dict[str, Any]]],
    item_id: Callable[[dict[str, Any]], int],
    payload: dict[str, Any],
    update_verb: str,
    item_url: Callable[[int], str],
    create_url: Callable[[], str],
    request: Callable[..., Any],
    errors: list[str],
    update_label: str,
    create_label: str,
) -> SummaryResult:
    """Find-or-create-then-update a single marker-tagged comment/note.

    Keeps the OLDEST existing match (`list_existing()[0]`) and deletes any
    duplicates — every provider's `_list_summary_comments`/`_list_summary_notes`
    already returns items in the order that makes index 0 correct for that
    provider (see each provider's `post_summary` for the ordering rationale);
    this helper does not re-order or re-interpret that ordering itself.
    """
    existing = list_existing()
    if existing:
        keep = existing[0]
        keep_id = item_id(keep)
        resp = request(update_verb, item_url(keep_id), json_body=payload)
        if resp.status_code >= 400:
            err = f"{update_label}: HTTP {resp.status_code}: {resp.text[:200]}"
            errors.append(err)
            return SummaryResult(comment_id=keep_id, created=False, updated=False, error=err)
        for dup in existing[1:]:
            request("DELETE", item_url(item_id(dup)))
        return SummaryResult(comment_id=keep_id, created=False, updated=True)

    resp = request("POST", create_url(), json_body=payload)
    if resp.status_code >= 400:
        err = f"{create_label}: HTTP {resp.status_code}: {resp.text[:200]}"
        errors.append(err)
        return SummaryResult(comment_id=None, created=False, updated=False, error=err)
    data = resp.json() or {}
    new_id = int(data.get("id", 0)) or None
    return SummaryResult(comment_id=new_id, created=True, updated=False)


def advance_sha_marker(
    *,
    list_existing: Callable[[], Sequence[dict[str, Any]]],
    item_id: Callable[[dict[str, Any]], int],
    extract_body: Callable[[dict[str, Any]], str],
    make_payload: Callable[[str], dict[str, Any]],
    update_verb: str,
    item_url: Callable[[int], str],
    request: Callable[..., Any],
    errors: list[str],
    new_sha: str,
    context_hint_prefix: str,
) -> bool:
    """Rewrite the `sha=` field in the existing summary marker in place.

    Returns True only if a summary comment/note was found AND its sha
    actually changed AND the patch HTTP call succeeded. A no-op sha (already
    at `new_sha`) returns False without making any HTTP call, same as every
    provider's pre-extraction behavior.
    """
    existing = list_existing()
    if not existing:
        return False
    keep = existing[0]
    keep_id = item_id(keep)
    old_body = extract_body(keep)
    new_body = replace_summary_sha(
        old_body, new_sha, context_hint=f"{context_hint_prefix}#{keep_id}"
    )
    if new_body == old_body:
        return False
    resp = request(update_verb, item_url(keep_id), json_body=make_payload(new_body))
    if resp.status_code >= 400:
        errors.append(f"advance_sha: HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    return True
