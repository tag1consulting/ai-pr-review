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

from ai_pr_review.slash.parser import (
    ParseError,
    SlashCommand,
    parse_commands,
)
from ai_pr_review.slash.parser import (
    _sanitize_reason as _sanitize_free_text,
)
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
# Stamped onto every ack reply this module posts (see apply_pending_verdicts'
# return-value construction below), and included in _OWN_MARKERS so a reply
# is never itself mistaken for a candidate command comment on a later run.
# Without this, a reply carries no ownership signal at all (the POST body is
# just {"content": {"raw": reply_text}}) -- the only thing stopping it from
# being re-parsed is that parse_commands requires "/ai-pr-review" at the
# start of a line, and the unsanitized commenter display_name interpolated
# into "@{actor} ..." (see _comment_actor's sanitization below) could in
# principle carry a newline that puts attacker-controlled text at
# line-start. If that text then read as a verdict command, _comment_account_id
# on the bot's OWN reply would resolve to the bot's own (write/admin) account
# -- laundering full verdict authority from an unprivileged comment into an
# apparently bot-authorized one. The marker closes this off unconditionally,
# independent of whether the newline theory is exploitable in practice.
_REPLY_MARKER_HIDDEN: Final[str] = "[//]: # (ai-pr-review-verdict-reply)"

_OWN_MARKERS: Final[tuple[str, ...]] = (
    SUMMARY_MARKER_PREFIX,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SKIP_MARKER,
    SKIP_MARKER_HIDDEN,
    ACKS_MARKER_HIDDEN_PREFIX,
    _REPLY_MARKER_HIDDEN,
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
    """The commenter's display name/nickname, sanitized before it's
    interpolated into a reply the BOT authors.

    `display_name`/`nickname` are fully user-controlled. Reusing
    `parser._sanitize_reason` (control chars stripped, newlines collapsed
    to spaces, length-capped, HTML-escaped) closes off both the direct
    risk (arbitrary markdown/mention text published under the bot's
    identity) and the newline-into-line-start risk `_REPLY_MARKER_HIDDEN`
    is the primary defense against -- this is defense in depth, not a
    substitute for that marker.
    """
    user = item.get("user") or {}
    raw = str(user.get("display_name") or user.get("nickname") or "there")
    sanitized = _sanitize_free_text(raw)
    return sanitized or "there"


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

    The `q=user.account_id="..."` filter is a server-side hint, not a
    trust boundary: this repo's own `bitbucket.py` documents that
    Bitbucket "sometimes ignores q on rich-text fields", and
    `user.account_id` is not among the fields Bitbucket's own docs list as
    filterable on this endpoint. If the filter is silently ignored, an
    unverified `values[0]` could belong to an arbitrary member of the
    repository's permission roster -- plausibly an admin -- which would
    grant "authorized" to any commenter regardless of their actual role.
    Every row is therefore re-verified client-side against `account_id`
    (and `repository.uuid`/`slug`, when present) before its `permission`
    is trusted; a response whose rows carry no `user` field at all to
    verify against is treated as "degraded", not "authorized" -- the same
    fail-closed posture as an outright HTTP or JSON failure, since "cannot
    verify" and "verified and failed" must never collapse into the same
    outcome as "verified and passed".
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

    saw_any_user_field = False
    matched_role: str | None = None
    for row in values:
        if not isinstance(row, dict):
            continue
        row_user = row.get("user")
        if not isinstance(row_user, dict) or "account_id" not in row_user:
            continue
        saw_any_user_field = True
        if row_user.get("account_id") != account_id:
            continue
        row_repo = row.get("repository")
        if isinstance(row_repo, dict) and "slug" in row_repo and row_repo.get("slug") != repo_slug:
            continue
        matched_role = str(row.get("permission") or "").lower()
        break

    if matched_role is None:
        if not saw_any_user_field:
            # The response has no `user` field on any row -- either the q
            # filter really did return a filtered-to-one-row response
            # (Bitbucket's REST 2.0 often omits echoing filter fields back)
            # or something unexpected about the response shape. Either
            # way, this can't be verified as belonging to `account_id`, so
            # it must not be trusted as if it did.
            _log.warning(
                "ai-pr-review: permission lookup for account %s returned "
                "rows with no verifiable user.account_id; cannot confirm "
                "the q filter was honored",
                account_id,
            )
            return "degraded"
        return "unauthorized"

    min_rank = _ROLE_RANK.get(min_role, _ROLE_RANK["write"])
    role_rank = _ROLE_RANK.get(matched_role, -1)
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
        # True only for a "degraded" check_authority() outcome -- a
        # transient permission-lookup failure that a LATER run's lookup
        # could plausibly resolve differently, unlike every other
        # rejection reason here (malformed command, missing F<n>, unknown
        # F<n>, or a comment with no account_id at all -- Bitbucket
        # comments don't gain a user field on a later fetch), which are
        # all durable properties of the comment itself that retrying gains
        # nothing from. Keeping the comment un-acked when this fires means
        # the next run's lookup gets a fresh chance instead of the
        # rejection becoming permanent on what may have been a one-off API
        # blip.
        comment_had_retryable_failure = False

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
                if check == "degraded":
                    comment_had_retryable_failure = True
                replies.append((comment_id, _reply_for_authority(actor, check)))
                continue

            verdict = _VERDICT_BY_CANONICAL_COMMAND[command.canonical_name]
            verdicts[fp] = verdict
            replies.append((comment_id, _reply_for_verdict(actor, command, verdict)))

        if comment_had_a_command and not comment_had_retryable_failure:
            newly_acked.append(comment_id)

    marked_replies = tuple(
        (comment_id, f"{text}\n\n{_REPLY_MARKER_HIDDEN}")
        for comment_id, text in replies
    )
    return VerdictPollResult(
        verdicts=verdicts,
        newly_acked_ids=tuple(newly_acked),
        replies=marked_replies,
        errors=tuple(errors),
    )
