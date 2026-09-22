"""Bitbucket parity Phase 4 (#874): poll the PR's own top-level comments for
verdict commands at review time.

Bitbucket-only, not a shared helper -- named _bitbucket_verdicts.py rather
than _verdicts.py for the same reason ai_pr_review.vcs._code_insights is
named the way it is (see that module's own docstring), so a contributor
doesn't mistake this for something github.py/gitlab.py also call into.

Bitbucket Pipelines has no comment-triggered event, so this module IS the
trigger: whatever `/ai-pr-review dismiss|false-positive|wont-fix|fixed F<n>`
commands are sitting in the PR's comment log get applied the next time the
review pipeline runs, not immediately. There is no inline-comment-reply
path the way GitHub has (`dismiss_inline_reply` alongside
`dismiss_by_finding_id`) -- Bitbucket findings anchor to the diff via Code
Insights annotations, which aren't repliable, so a verdict command is
always a fresh top-level comment referencing an `F<n>` token, never a
reply to one. This maps onto GitHub's existing *body-level* dismiss shape
(`ai_pr_review.slash.github_ops.dismiss_by_finding_id`'s BODY branch), not
its inline-thread-reply sibling.

Authorization is fail-closed by design (issue #874's motivating case was a
secret-scanner match that needed dismissing, and a false accept here would
let any commenter silence a security finding): a permission-lookup failure
rejects the verdict, it never falls back to accepting it. This is the one
place in the Bitbucket-parity plan where fail-soft is the wrong default.

Idempotency has two independent halves. Suppression is idempotent for
free: writing the same fingerprint -> "dismissed" entry twice through
`marker.upsert_verdicts_marker` produces the same result either time.
Acknowledgement replies are not automatically idempotent the same way --
without the acks marker (`marker.build_acks_marker`/`extract_acks`), the
next run would re-parse the same command comment and post a second reply.
Rejected alternative for tracking acks: checking whether a command comment
already has a bot child reply, which costs an extra GET per command and
silently breaks if the reply POST succeeds but this run's body write
(carrying the updated acks marker) fails -- the ack set already rides
along in a body `post_findings` rewrites every run anyway, so recording it
there is free.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from ai_pr_review.slash.parser import ParseError, SlashCommand, parse_commands
from ai_pr_review.vcs._finding_ids import fingerprint_for_finding_id
from ai_pr_review.vcs.http import RecordingClient
from ai_pr_review.vcs.marker import (
    ACKS_MARKER_HIDDEN_PREFIX,
    SKIP_MARKER,
    SKIP_MARKER_HIDDEN,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SUMMARY_MARKER_PREFIX,
    extract_acks,
)

_log = logging.getLogger(__name__)

# Cap how far back a single run scans, matching the acks marker's own
# bounded-growth cap (marker.py's _MAX_ACKED_IDS) -- a truly ancient verdict
# comment beyond this window is not silently honored forever, it just needs
# a rescan (add the ai-review-rescan label, or re-post the command) to be
# picked up, the same tradeoff every bounded-history mechanism in this repo
# makes.
_MAX_COMMENTS_SCANNED: Final[int] = 200

# Marker-only ownership gating (bitbucket.py's module docstring: "No author
# info exposed uniformly on comments -> marker-only ownership gating").
# A comment carrying any marker we write ourselves is never a candidate
# command comment to parse, regardless of what literal text it also
# contains.
_OWN_MARKERS: Final[tuple[str, ...]] = (
    SUMMARY_MARKER_PREFIX,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SKIP_MARKER,
    SKIP_MARKER_HIDDEN,
    ACKS_MARKER_HIDDEN_PREFIX,
)

Verdict = Literal["dismissed", "fixed"]
_VERDICT_BY_CANONICAL_COMMAND: Final[dict[str, Verdict]] = {
    "false-positive": "dismissed",
    "wont-fix": "dismissed",
    "fixed": "fixed",
}

AuthorityCheck = Literal["authorized", "unauthorized", "degraded"]
_ROLE_RANK: Final[dict[str, int]] = {"read": 0, "write": 1, "admin": 2}


@dataclass(frozen=True)
class VerdictPollResult:
    """Outcome of one polling pass over the PR's comments.

    ``verdicts`` is the full fingerprint -> verdict map after applying any
    newly-found commands (callers should merge this over, not alongside,
    whatever `extract_verdicts` already returned from the same body).
    ``newly_acked_ids`` are comment ids to add to the acks marker.
    ``replies`` are (comment_id, reply_text) pairs the caller should post,
    one per processed command comment -- rejections included, so a
    legitimately-authorized user hitting a transient permissions-API
    failure can tell "you're not allowed" from "could not verify, retry."
    """

    verdicts: dict[str, str] = field(default_factory=dict)
    newly_acked_ids: tuple[int, ...] = ()
    replies: tuple[tuple[int, str], ...] = ()
    errors: tuple[str, ...] = ()


def _comment_actor(item: dict[str, Any]) -> str:
    user = item.get("user") or {}
    return str(user.get("display_name") or user.get("nickname") or "there")


def _comment_account_id(item: dict[str, Any]) -> str | None:
    user = item.get("user") or {}
    account_id = user.get("account_id")
    return str(account_id) if account_id else None


def _is_own_comment(body: str) -> bool:
    return any(marker in body for marker in _OWN_MARKERS)


def check_authority(
    client: RecordingClient,
    *,
    workspace: str,
    repo_slug: str,
    account_id: str,
    min_role: str,
) -> AuthorityCheck:
    """Check whether `account_id` holds at least `min_role` on this repo.

    Fail-closed: any lookup failure (non-2xx, malformed JSON, no matching
    permission row) returns something other than "authorized". Only a
    clean 2xx response whose permission rank meets or exceeds `min_role`
    returns "authorized". "degraded" (vs. "unauthorized") distinguishes a
    lookup that could not be completed from one that completed and found
    the commenter genuinely lacks access -- callers use this to word the
    rejection reply differently (issue #874's explicit requirement: a
    silent rejection is indistinguishable from the bot ignoring the
    command).
    """
    url = f"/workspaces/{workspace}/permissions/repositories/{repo_slug}"
    params = {"q": f'user.account_id="{account_id}"'}
    resp = client.request("GET", url, params=params)
    if resp.status_code >= 400:
        _log.warning(
            "ai-pr-review: permission lookup failed for account %s: HTTP %d",
            account_id, resp.status_code,
        )
        return "degraded"
    try:
        data = resp.json() or {}
    except ValueError as exc:
        _log.warning(
            "ai-pr-review: permission lookup for account %s returned "
            "unparseable JSON: %s", account_id, exc,
        )
        return "degraded"
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return "unauthorized"
    role = str((values[0] or {}).get("permission") or "").lower()
    min_rank = _ROLE_RANK.get(min_role, _ROLE_RANK["write"])
    role_rank = _ROLE_RANK.get(role, -1)
    return "authorized" if role_rank >= min_rank else "unauthorized"


def _reply_for_parse_error(actor: str, err: ParseError) -> str:
    token = err.unknown_token or "?"
    return (
        f"@{actor} `{token}` is not a recognized `/ai-pr-review` command. "
        "Verdict commands on Bitbucket must reference a finding, e.g. "
        "`/ai-pr-review dismiss F3 <reason>`."
    )


def _reply_for_missing_finding_id(actor: str, command: SlashCommand) -> str:
    return (
        f"@{actor} `/ai-pr-review {command.name}` needs a finding ID on "
        "Bitbucket, e.g. `/ai-pr-review dismiss F3 <reason>` -- there is no "
        "inline comment to reply to that would otherwise identify which "
        "finding this targets."
    )


def _reply_for_unknown_finding_id(actor: str, finding_id: int) -> str:
    return (
        f"@{actor} F{finding_id} was not found in the current review. It "
        "may be from an older run, or already resolved."
    )


def _reply_for_authority(actor: str, check: AuthorityCheck) -> str:
    if check == "unauthorized":
        return (
            f"@{actor} you need write access to this repository to use "
            "`/ai-pr-review` commands, so this one was not applied."
        )
    return (
        f"@{actor} could not verify your repository permissions right now, "
        "so this command was not applied. Try again, or ask someone with "
        "write access to re-run the review."
    )


def _reply_for_verdict(actor: str, command: SlashCommand, verdict: Verdict) -> str:
    if verdict == "fixed":
        sha_note = f" (commit `{command.commit_sha}`)" if command.commit_sha else ""
        return (
            f"@{actor} marked **F{command.finding_id}** as `fixed`{sha_note}. "
            "It will be re-evaluated on the next review run."
        )
    reply = (
        f"@{actor} marked **F{command.finding_id}** as `{command.name}`. "
        "This finding is suppressed on this PR. This does not write to the "
        "cross-repo learning-loop store on Bitbucket yet -- see "
        "docs/learning-loop.md."
    )
    return reply


def apply_pending_verdicts(
    client: RecordingClient,
    *,
    workspace: str,
    repo_slug: str,
    comments: Sequence[dict[str, Any]],
    existing_body: str,
    min_role: str,
) -> VerdictPollResult:
    """Poll `comments` (already fetched by the caller, see bitbucket.py's
    `_fetch_comments` cache) for `/ai-pr-review` verdict commands and apply
    any new ones.

    `existing_body` is the current summary comment body, used both to
    resolve `F<n>` -> fingerprint (`fingerprint_for_finding_id`) and to
    read which comment ids are already acknowledged (`extract_acks`) so a
    rerun never double-replies.
    """
    from ai_pr_review.vcs.marker import extract_verdicts

    verdicts = dict(extract_verdicts(existing_body))
    already_acked = extract_acks(existing_body)
    newly_acked: list[int] = []
    replies: list[tuple[int, str]] = []
    errors: list[str] = []
    authority_cache: dict[str, AuthorityCheck] = {}

    for item in comments[:_MAX_COMMENTS_SCANNED]:
        comment_id = item.get("id")
        if not isinstance(comment_id, int):
            continue
        if comment_id in already_acked:
            continue
        body = ((item.get("content") or {}).get("raw")) or ""
        if _is_own_comment(body):
            continue
        parsed = parse_commands(body)
        if not parsed:
            continue

        actor = _comment_actor(item)
        account_id = _comment_account_id(item)
        comment_had_a_command = False

        for result in parsed:
            if isinstance(result, ParseError):
                replies.append((comment_id, _reply_for_parse_error(actor, result)))
                comment_had_a_command = True
                continue

            command = result
            if command.canonical_name not in _VERDICT_BY_CANONICAL_COMMAND:
                # Not a verdict command (explain/revise/feedback) -- out of
                # scope for this poller, which only ever applies
                # dismiss/false-positive/wont-fix/fixed. Leave it alone
                # entirely: don't ack it, don't reply, so a future phase
                # that does handle these commands isn't starved of seeing
                # them by this one having already marked the comment acked.
                continue

            comment_had_a_command = True
            if command.finding_id is None:
                replies.append(
                    (comment_id, _reply_for_missing_finding_id(actor, command))
                )
                continue

            fp = fingerprint_for_finding_id([existing_body], command.finding_id)
            if fp is None:
                replies.append(
                    (comment_id, _reply_for_unknown_finding_id(actor, command.finding_id))
                )
                continue

            if account_id is None:
                replies.append((comment_id, _reply_for_authority(actor, "degraded")))
                errors.append(
                    f"apply_pending_verdicts: comment {comment_id} has no "
                    "account_id, treating as a failed permission check"
                )
                continue

            check = authority_cache.get(account_id)
            if check is None:
                check = check_authority(
                    client, workspace=workspace, repo_slug=repo_slug,
                    account_id=account_id, min_role=min_role,
                )
                authority_cache[account_id] = check
            if check != "authorized":
                replies.append((comment_id, _reply_for_authority(actor, check)))
                continue

            verdict = _VERDICT_BY_CANONICAL_COMMAND[command.canonical_name]
            verdicts[fp] = verdict
            replies.append((comment_id, _reply_for_verdict(actor, command, verdict)))

        if comment_had_a_command:
            newly_acked.append(comment_id)

    return VerdictPollResult(
        verdicts=verdicts,
        newly_acked_ids=tuple(newly_acked),
        replies=tuple(replies),
        errors=tuple(errors),
    )
