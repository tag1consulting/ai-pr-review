"""GitHub slash-command HTTP/GraphQL orchestration — the calling half of
`ai_pr_review.slash.github_orchestration` (issue #849 split).

Split out of `github_orchestration.py` (~1,600 lines pre-split, itself
renamed from `dismiss.py` in #833) once that module's own docstring
started listing #849 as unfinished business: the module mixed two
genuinely different concerns that happened to share one file --

- `github_orchestration.py` (kept): pure F-ID/body classification, reply-text
  templates, and the thin CLI-adjacent helpers that need no HTTP mocking to
  test (`tests/python/slash/test_github_orchestration.py` /
  `test_github_orchestration_cli_helpers.py`).
- `github_ops.py` (this module): everything that actually calls a
  `GitHubProvider`'s HTTP/GraphQL methods to resolve threads, dismiss or
  approve reviews, record verdict markers, or fetch a parent comment --
  tested via the `RecordingClient`/`TapeRecorder` HTTP-mock harness in
  `tests/python/vcs/test_github_orchestration_http.py` /
  `test_github_orchestration_verdicts.py`.

This module imports classification primitives from
`ai_pr_review.slash.github_orchestration` (one-directional: this module
depends on that one, never the reverse) -- see that module's docstring for
why the dependency runs this way rather than the other, and why both halves
stayed under `ai_pr_review.slash` rather than moving into `ai_pr_review.vcs`
despite the HTTP-mocked tests living in `tests/python/vcs/`.

GitHub-only: GitLab and Bitbucket have no F-ID / id-map system.
"""

from __future__ import annotations

import logging
import os as _os
import re
import sys as _sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

from ai_pr_review.findings.scope import is_analyzer_source
from ai_pr_review.slash.github_orchestration import (
    _ANALYZER_SUPPRESSION_HINT,
    _BOT_LOGIN,
    _FIXED_NO_APPROVE_NOTE,
    DismissResult,
    FeedbackContext,
    FindingLocation,
    _fingerprint_for_finding_id,
    _inline_feedback_context,
    _not_found_reply,
    _sha_citation,
    _thread_by_comment_id,
    bodies_newest_first,
    classify_finding,
    context_from_body_finding_id,
    list_active_body_ids,
    parse_inline_comment_header,
)
from ai_pr_review.vcs._finding_ids import _ID_RE
from ai_pr_review.vcs._stale import is_owned_by_us
from ai_pr_review.vcs._thread import (
    count_unresolved_owned_threads,
)
from ai_pr_review.vcs._thread import (
    first_comment_author_login as _first_comment_author_login,
)
from ai_pr_review.vcs._thread import (
    first_comment_body as _first_comment_body,
)
from ai_pr_review.vcs._thread import (
    first_comment_id as _first_comment_id,
)
from ai_pr_review.vcs._thread import (
    first_comment_review_id as _thread_review_id,
)
from ai_pr_review.vcs.marker import extract_inline_meta, upsert_verdicts_marker

if TYPE_CHECKING:
    from ai_pr_review.slash.parser import SlashCommand
    from ai_pr_review.vcs.github import GitHubProvider

_log = logging.getLogger(__name__)


def _record_verdict(
    provider: GitHubProvider,
    reviews: Sequence[dict[str, Any]],
    fingerprint: str | None,
    verdict: str,
    *,
    resolved_comment_id: int | None = None,
) -> None:
    """Best-effort: patch the canonical review's verdict marker.

    `resolved_comment_id`, when given, is the databaseId of the inline
    comment whose thread was just resolved by this same call — if the
    canonical body's "Still open from earlier reviews" section still names
    it (via `github.strip_carried_forward_entry`), that bullet is removed in
    the same PUT so the visible text doesn't contradict the verdict marker
    it sits next to (#811: a resolved thread's own review kept reading
    "Still open" until the next full review run). `None` for a body-level
    finding, which was never rendered there in the first place.

    "Canonical" = the most recently posted bot review (highest `id`) with a
    non-empty body among `reviews`, regardless of its current state (a
    dismissed review is still fully visible and still the last thing this
    bot posted, dismissal doesn't delete or hide it). A review with an
    empty body is skipped: GitHub auto-creates one of these whenever the
    bot replies to an inline comment, and rejects any attempt to `PUT` a
    body onto it.

    Deliberately swallows every failure into a log line (and a GitHub
    Actions `::warning::` annotation when running in CI), never raising and
    never surfacing into the caller's `DismissResult.errors`. This is a new,
    additive side-channel layered onto an already-working resolve/dismiss/
    approve flow (Epic 13); a verdict-recording failure must never change
    whether a thread is reported as resolved, since it changes nothing about
    the primary outcome. The only downstream effect of a swallowed failure
    is that a future review cycle's cross-run classification (per the
    PR-comment-clutter design) won't see this verdict -- degrading to
    "treat as unmatched" for that one finding, not a correctness break.

    The `except httpx.HTTPError` below is load-bearing, not decorative:
    `provider.update_review_body` routes through `RecordingClient.request` ->
    `retry_transient`, which *raises* `RetryExhaustedError` (an `httpx.
    HTTPError` subclass) on retry exhaustion or re-raises the underlying
    transport exception outright -- it does not always hand back a tidy
    `(ok, status, snippet)` tuple the way a plain non-2xx response does. Only
    catching that tuple's `ok is False` case (as an earlier version of this
    function did) leaves a network blip free to propagate all the way out of
    `dismiss_by_finding_id`/`dismiss_inline_reply` and crash the CLI command
    -- after the thread has already been resolved on GitHub -- which is
    exactly the outcome this function's docstring promises never happens.

    Seeds the verdicts map from `merge_verdicts(reviews)` -- the union
    across every prior bot review body -- rather than only the canonical
    body's own `extract_verdicts`. Reading just the canonical body would
    silently drop every earlier verdict whenever a marker-less review (e.g.
    `GitHubProvider.submit_approval`'s human-facing "auto-approved" message,
    issue #590) becomes canonical between one verdict write and the next.
    `select_canonical` centralizes the "highest id with a non-empty body,
    any state" rule so this function and the read side
    (`ai_pr_review.vcs._canonical`, which consumes these verdicts during
    `post_findings` classification) can never disagree about which review
    is canonical.
    """
    from ai_pr_review.vcs._canonical import merge_verdicts, select_canonical

    if fingerprint is None:
        return
    if not reviews:
        return
    canonical = select_canonical(reviews)
    if canonical is None:
        # Every review's body is empty (e.g. the bot has only ever posted
        # reply-created reviews on this PR so far). Warn rather than
        # returning silently: this function's own docstring promises every
        # failure surfaces as a log line, and a caller regressing the
        # invariant that a non-None fingerprint implies at least one
        # non-empty review body would otherwise drop a verdict with zero
        # trace anywhere, reproducing the exact bug class this side channel
        # exists to prevent.
        _warn_verdict_failure(
            f"skipping '{verdict}' verdict: no canonical review with a "
            f"non-empty body among {len(reviews)} review(s)"
        )
        return
    canonical_id = canonical.review_id
    verdicts = merge_verdicts(reviews)
    verdicts[fingerprint] = verdict
    new_body = upsert_verdicts_marker(canonical.body, verdicts)
    if resolved_comment_id is not None:
        from ai_pr_review.vcs.github import strip_carried_forward_entry

        new_body = strip_carried_forward_entry(new_body, resolved_comment_id)
    try:
        ok, status, snippet = provider.update_review_body(canonical_id, new_body)
    except httpx.HTTPError as exc:
        _warn_verdict_failure(
            f"failed to record '{verdict}' verdict on review {canonical_id}: {exc!r}"
        )
        return
    if ok:
        return
    _warn_verdict_failure(
        f"failed to record '{verdict}' verdict on review {canonical_id}: "
        f"HTTP {status}: {snippet}"
    )


def _warn_verdict_failure(message: str) -> None:
    """Emit the verdict-recording-failure annotation to stderr, never stdout.

    `dismiss`/`dismiss-inline` (ai_pr_review/cli.py) print the human-facing
    reply to stdout, which the calling workflow captures verbatim
    (`reply=$(ai-pr-review ...)`, `.github/workflows/slash-commands.yml`)
    and posts as the PR comment. A `print()` to stdout here would land this
    `::warning::` annotation inside that posted comment instead of the
    Actions log — confirmed live: an early version of this function did
    exactly that, and a user's `/ai-pr-review dismiss F<n>` reply arrived
    with a raw HTTP-422 warning line glued onto the front of it. Every other
    `::warning::`/`::notice::` annotation in cli.py already goes to stderr
    via `click.echo(..., err=True)`; this matches that contract.
    """
    if _os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::ai-pr-review: {message}", file=_sys.stderr, flush=True)
    _log.warning("dismiss: %s", message)


def _dismiss_if_all_resolved(
    provider: GitHubProvider,
    threads: Sequence[dict[str, Any]],
    target_review_id: int,
    *,
    dismiss_message: str,
) -> tuple[bool, list[str]]:
    """Dismiss target_review_id iff none of its own threads remain unresolved
    AND the review is currently `CHANGES_REQUESTED`.

    The state check (issue #562) matches the original bash job's
    `if review_state != CHANGES_REQUESTED: skip` guard, ported here rather
    than at either call site so both `dismiss_by_finding_id` (story 13-2) and
    `dismiss_inline_reply` (story 13-3) get the fix from one place. Without
    it, resolving the last unresolved thread on an already-`DISMISSED` (or
    `APPROVED`/`COMMENTED`) review attempts a dismiss PUT GitHub correctly
    rejects — not silently swallowed since story 13-3 (`DismissResult.errors`
    surfaces it), but still a wasted API call for a case that should be a
    clean no-op. A state-fetch failure fails closed: skip the dismiss and
    surface an error, rather than guessing and risking a wrongful dismiss
    attempt on unverifiable state.

    Count scope is always per-review (databaseId == target_review_id), never
    PR-wide, per the canonical semantics chosen in Epic 13's design (the 4
    existing bash copies disagreed on this).

    Deliberately passes `bot_login=None` to `is_owned_by_us` (author-login
    check skipped, marker is the sole gate) for GraphQL-sourced author logins
    specifically. This differs from `resolve_stale`/`_dismiss_stale_reviews`/
    `_load_prior_state`'s `parse_prior_thread` call, which pass
    `graphql_bot_login(self.config.bot_login)` — the REST-style
    "github-actions[bot]" constant with the "[bot]" suffix stripped — against
    the same GraphQL-sourced author field (issue #717, confirmed live: the
    format difference between GraphQL's and REST's bot-login string is a
    settled fact, not a hypothesis — see `graphql_bot_login()`'s own
    docstring in `_stale.py`). Choosing `None` here is still correct given
    that: unlike those three call sites, this function does not need the
    extra defense-in-depth signal to be safe, because a spoofed thread
    carries the attacker's own `pullRequestReview.databaseId`, so at most it
    could trigger dismissal of the attacker's own review, not ours. This is a
    genuine, intentional inconsistency between the two groups of call sites
    (not a bug in either), and `test_dismiss_inline_reply_graphql_style_author_still_owned`
    (`tests/python/vcs/test_github_orchestration_http.py`) pins it: it fails on purpose
    if `None` is ever swapped for a real `bot_login` here without that
    tradeoff being re-examined first.
    """
    errors: list[str] = []
    counts = count_unresolved_owned_threads(threads, bot_login=None)
    if counts.get(target_review_id, 0) > 0:
        return False, errors

    state = provider.get_review_state(target_review_id)
    if state is None:
        errors.append(f"get_review_state {target_review_id}: could not verify review state, skipping dismiss")
        return False, errors
    if state != "CHANGES_REQUESTED":
        # Not an error: the review is already dismissed/approved/commented,
        # so there is nothing to do. Silent, matching the bash guard this
        # ports — a skip here is the correct, expected outcome.
        return False, errors

    ok, status, body_snippet = provider.dismiss_review(target_review_id, dismiss_message)
    if not ok:
        errors.append(f"dismiss review {target_review_id}: HTTP {status}: {body_snippet}")
        return False, errors
    return True, errors


def _approve_if_pr_fully_resolved(
    provider: GitHubProvider,
    threads: Sequence[dict[str, Any]],
    *,
    approve_allowed: bool,
    dismiss_message: str,
    approve_message: str,
) -> tuple[bool, list[str]]:
    """Approve the PR iff EVERY active bot-authored `CHANGES_REQUESTED` review
    has zero unresolved findings left — not just the review whose thread the
    caller just resolved (issue #590).

    This is deliberately PR-wide, diverging from `_dismiss_if_all_resolved`'s
    per-review scope: mirrors `resolve_stale`/`_dismiss_stale_reviews`'s
    existing PR-wide semantics for stale-review cleanup, applied here to the
    "should we now approve" question. A single dismiss/false-positive/wont-fix
    call only clears one thread (and, via `_dismiss_if_all_resolved`, at most
    one review) — this function separately re-checks the *entire* PR's bot
    review set before deciding to approve, so a PR with several outstanding
    CHANGES_REQUESTED reviews from successive review cycles is only approved
    once all of them are clear, not the moment any single one empties out.

    `approve_allowed` gates the entire operation: callers pass the
    trust-boundary decision (issue #590's tighter OWNER/MEMBER bar for the
    auto-approve escalation, stricter than plain dismiss's COLLABORATOR
    level) in from outside, so this function stays free of any actor/
    author-association knowledge. When False, this is a cheap no-op
    (`(False, [])`) — no extra API calls are made for actors who cannot
    trigger this behavior.

    Race safety: re-fetches `list_bot_reviews()` immediately before deciding,
    the same "verify state right before acting" pattern
    `_dismiss_if_all_resolved` uses via `get_review_state` — a concurrent push
    landing a new finding between the caller's thread-resolve and this check
    will show up as a new CHANGES_REQUESTED review (or new unresolved threads
    on an existing one) and abort the approve.

    Returns `(approved, errors)`. Never dismisses or approves if any bot
    review fetch/list call fails (fails closed, matching
    `_dismiss_if_all_resolved`'s get_review_state failure handling).

    Scope note: "fully resolved" is measured by unresolved *inline thread*
    count only, same as `_dismiss_stale_reviews`'s existing PR-wide
    stale-review cleanup (which also does not check for outstanding
    body-level findings before dismissing). A CHANGES_REQUESTED review whose
    only remaining findings are body-level (no backing GraphQL thread) will
    be treated as clear once its inline threads are resolved. This matches
    established precedent rather than introducing new behavior; body-level
    findings require an explicit `F<n>` command to dismiss in the first
    place, so this is a pre-existing, documented scope boundary, not a gap
    unique to the approve path.
    """
    if not approve_allowed:
        return False, []

    errors: list[str] = []

    # Count unresolved, marker-owned threads per owning review, PR-wide (not
    # filtered to a single target_review_id, unlike _dismiss_if_all_resolved).
    unresolved_by_review = count_unresolved_owned_threads(threads, bot_login=None)

    reviews = provider.list_bot_reviews()
    cr_review_ids = [
        rid
        for r in reviews
        if r.get("state") == "CHANGES_REQUESTED" and (rid := r.get("id")) is not None
    ]
    if not cr_review_ids:
        # Nothing to approve over: either there was never a CHANGES_REQUESTED
        # review (e.g. this call is racing a state we don't own) or it was
        # already cleared by a prior call. Silent no-op, matching
        # _dismiss_if_all_resolved's "already not CHANGES_REQUESTED" skip.
        return False, errors

    for rid in cr_review_ids:
        if unresolved_by_review.get(int(rid), 0) > 0:
            # At least one CHANGES_REQUESTED review still has our own
            # unresolved findings -- not all-clear PR-wide yet.
            return False, errors

    dismissed_ids: list[int] = []
    for rid in cr_review_ids:
        # Re-verify state immediately before dismissing each review -- the
        # same race guard _dismiss_if_all_resolved applies per-review,
        # repeated here per-review across the whole PR-wide set so a
        # concurrent push that flips one of several reviews out of
        # CHANGES_REQUESTED between the list above and this loop is not
        # dismissed a second time.
        state = provider.get_review_state(int(rid))
        if state is None:
            errors.append(f"get_review_state {rid}: could not verify review state, skipping approve")
            return False, errors
        if state != "CHANGES_REQUESTED":
            continue
        ok, status, body_snippet = provider.dismiss_review(int(rid), dismiss_message)
        if not ok:
            errors.append(f"dismiss review {rid}: HTTP {status}: {body_snippet}")
            return False, errors
        dismissed_ids.append(int(rid))

    if not dismissed_ids:
        # Every CR review flipped state under us between the list and the
        # per-review re-check (e.g. dismissed by a concurrent run) -- nothing
        # left for us to approve over.
        return False, errors

    ok, status, body_snippet = provider.submit_approval(approve_message)
    if not ok:
        # dismissed_ids were already committed via real dismiss_review() API
        # calls above and cannot be rolled back -- name them so the caller's
        # reply/log surfaces the inconsistent state (dismissed but not
        # approved) instead of only reporting the submit_approval failure.
        errors.append(
            f"submit_approval: HTTP {status}: {body_snippet} "
            f"(reviews already dismissed without a completed approval: {dismissed_ids})"
        )
        return False, errors
    return True, errors


def dismiss_by_finding_id(
    provider: GitHubProvider,
    finding_id: int,
    *,
    actor: str,
    command: str,
    approve_allowed: bool = False,
    commit_sha: str = "",
) -> DismissResult:
    """Handle `/ai-pr-review dismiss|false-positive|wont-fix|fixed F<n>` from
    a top-level PR comment (no parent review comment to reply to).

    BODY findings: no thread to resolve or review to dismiss (there is no
    single GraphQL thread backing a body-level finding). For
    dismiss/false-positive/wont-fix, the caller is expected to record a
    feedback-store entry so the finding is suppressed on the next re-run; for
    `fixed` the caller must NOT do this (see SlashCommand.is_feedback_command)
    and the reply below says so honestly rather than claiming suppression.

    INLINE findings: resolve the thread carrying `**[F<n>]**` gated by our own
    inline marker (never touch another bot's or human's thread), then dismiss
    that thread's owning review if all of its own threads are now resolved.
    When `approve_allowed` (issue #590's tighter trust gate, decided by the
    caller from the actor's author association), also checks whether this
    resolution cleared the *last* active finding PR-wide across all bot
    CHANGES_REQUESTED reviews, and if so submits a fresh APPROVE review. The
    caller must pass `approve_allowed=False` unconditionally for `command=
    "fixed"` -- a fix claim is not a maintainer verdict and should not trigger
    auto-approval; see cli.py's `dismiss`/`dismiss-inline` commands, which
    enforce this override regardless of the actor's association.

    `commit_sha` (only meaningful for `command="fixed"`) is echoed bare in
    the reply so GitHub auto-links it to the commit; it is never validated
    against the repo and plays no role in resolution or dismissal.

    UNKNOWN: no action, reply says so.
    """
    # Snapshot before any sub-call writes to provider._errors (e.g. an HTTP
    # error or a GraphQL-200-with-errors body — the #555 failure class); all
    # new entries are drained via provider._errors[errors_before:] below so
    # they cannot be silently lost the way the bash `gh api --jq` call lost
    # them.
    errors_before = len(provider._errors)
    errors: list[str] = []

    reviews = provider.list_bot_reviews()
    bodies = bodies_newest_first(reviews)
    classified = classify_finding(bodies, finding_id)

    if classified.location is FindingLocation.UNKNOWN:
        errors.extend(provider._errors[errors_before:])
        return DismissResult(
            reply=_not_found_reply(actor, finding_id, had_errors=bool(errors)),
            errors=tuple(errors),
        )

    if classified.location is FindingLocation.BODY:
        errors.extend(provider._errors[errors_before:])
        fingerprint = _fingerprint_for_finding_id(bodies, finding_id)
        if command == "fixed":
            # No thread exists for a body-level finding, and "fixed" must
            # never write a feedback-store entry (it isn't a verdict on
            # whether the finding was valid) -- so there's nothing to resolve
            # or dismiss. It IS recorded, though: the verdict marker is what
            # makes "it will be re-evaluated on the next review run" true --
            # a recurring exact-fingerprint match gets re-surfaced rather
            # than silently treated as brand new. feedback_source/file/
            # rule_id are deliberately left empty so cli.py's is_body_finding
            # check doesn't route this into the feedback-store persistence
            # path.
            _record_verdict(provider, reviews, fingerprint, "fixed")
            return DismissResult(
                reply=(
                    f"@{actor} marked **F{finding_id}** as `fixed`"
                    f"{_sha_citation(commit_sha)}. There is no review thread for a "
                    "body-level finding to resolve; it will be re-evaluated on the "
                    "next review run."
                ),
                acted=True,
                active_body_ids=tuple(list_active_body_ids(bodies)),
                errors=tuple(errors),
            )
        _record_verdict(provider, reviews, fingerprint, "dismissed")
        reply = (
            f"@{actor} marked **F{finding_id}** as `{command}`. "
            "This finding will be suppressed on future review runs."
        )
        if is_analyzer_source(classified.source):
            reply += _ANALYZER_SUPPRESSION_HINT
        return DismissResult(
            reply=reply,
            feedback_source=classified.source,
            feedback_file=classified.file,
            feedback_rule_id=classified.rule_id,
            feedback_finding_id=finding_id,
            feedback_eligible=True,
            acted=True,
            active_body_ids=tuple(list_active_body_ids(bodies)),
            errors=tuple(errors),
        )

    # INLINE: find the thread carrying this finding, gated by our own inline
    # marker (never touch another bot's or human's thread).
    #
    # F-ids are assigned fresh per review run, not globally unique (#787): a
    # thread resolved without a verdict (#779) can recur as a brand-new
    # duplicate thread on a later rescan, and both may independently render
    # as `[F1]` in their own review's numbering even though they're separate
    # GitHub threads. Matching on that visible label alone can silently
    # resolve a stale duplicate while leaving the genuinely open thread
    # untouched. So candidates are matched by fingerprint first (decoded
    # from each thread's own metadata marker via `extract_inline_meta`,
    # checking `prior_fps` too for #720 drift) and only fall back to the
    # `[F<n>]` substring for a legacy/markerless comment that predates the
    # marker or when `target_fp` itself couldn't be resolved. Among
    # candidates, an unresolved thread is preferred over an already-resolved
    # one -- picking a resolved match instead would repeat exactly the #787
    # bug this exists to fix.
    threads = provider.fetch_review_threads()
    target_fp = _fingerprint_for_finding_id(bodies, finding_id)
    candidates: list[dict[str, Any]] = []
    for t in threads:
        body = _first_comment_body(t)
        author = _first_comment_author_login(t) or None
        if not is_owned_by_us(body, author, None, kind="inline"):
            continue
        meta = extract_inline_meta(body)
        if target_fp is not None and meta is not None:
            if meta.fp != target_fp and target_fp not in meta.prior_fps:
                continue
        elif f"[F{finding_id}]" not in body:
            continue
        candidates.append(t)

    target_thread = next((t for t in candidates if not t.get("isResolved")), None)
    if target_thread is None and candidates:
        target_thread = candidates[0]

    if target_thread is None:
        errors.extend(provider._errors[errors_before:])
        return DismissResult(
            reply=_not_found_reply(actor, finding_id, had_errors=bool(errors)),
            errors=tuple(errors),
        )

    thread_id = target_thread.get("id")
    resolved = False
    if not target_thread.get("isResolved") and isinstance(thread_id, str):
        ok, status, body_snippet = provider.resolve_thread(thread_id)
        if ok:
            resolved = True
            # Update the snapshot in place (target_thread is a live reference
            # into `threads`) so `_dismiss_if_all_resolved` sees this thread as
            # resolved without a second GraphQL fetch. A re-fetch would be a
            # new #555 surface: a GraphQL-200-with-errors response there would
            # read as "zero unresolved threads" and cause an erroneous dismiss.
            target_thread["isResolved"] = True
        else:
            errors.append(f"resolve thread {thread_id}: HTTP {status}: {body_snippet}")
    else:
        resolved = bool(target_thread.get("isResolved"))

    review_dismissed = False
    pr_approved = False
    review_id = _thread_review_id(target_thread)
    if resolved:
        _record_verdict(
            provider,
            reviews,
            target_fp,
            "fixed" if command == "fixed" else "dismissed",
            resolved_comment_id=_first_comment_id(target_thread),
        )
        # Try the PR-wide approve path FIRST: it is the sole dismisser for
        # the reviews it clears (it dismisses each CHANGES_REQUESTED review
        # itself before submitting the APPROVE). Running
        # `_dismiss_if_all_resolved` beforehand would dismiss review_id ahead
        # of the PR-wide check, so by the time `_approve_if_pr_fully_resolved`
        # re-lists reviews via `list_bot_reviews()`, that review would already
        # read back as DISMISSED — `cr_review_ids` would be empty and the
        # approve would never fire even in the common single-review case. When
        # `approve_allowed` is False, or the PR-wide check finds it isn't
        # fully clear yet (or races and dismisses nothing), this is a cheap
        # no-op and falls through to the existing per-review dismiss so the
        # normal (non-approving) dismiss behavior is unaffected.
        if approve_allowed:
            pr_approved, approve_errors = _approve_if_pr_fully_resolved(
                provider,
                threads,
                approve_allowed=approve_allowed,
                dismiss_message="Superseded: all findings resolved via slash command.",
                approve_message=f"@{actor} cleared the last active finding via `/ai-pr-review {command}`.",
            )
            errors.extend(approve_errors)

        if pr_approved:
            review_dismissed = True
        elif review_id is not None:
            # Not PR-wide clear (or the approve attempt raced and dismissed
            # nothing) — fall back to dismissing just this review.
            # `_dismiss_if_all_resolved`'s own `get_review_state` guard makes
            # this a safe no-op if `_approve_if_pr_fully_resolved` already
            # dismissed review_id as part of a PR-wide set that didn't end up
            # fully clear (state will read back as something other than
            # CHANGES_REQUESTED).
            review_dismissed, dismiss_errors = _dismiss_if_all_resolved(
                provider,
                threads,
                review_id,
                dismiss_message="Superseded: all findings resolved via slash command.",
            )
            errors.extend(dismiss_errors)

    errors.extend(provider._errors[errors_before:])

    # Full-context feedback entry for an INLINE finding named on a top-level
    # comment (issue #769): the thread carries its own source/rule_id/file
    # with no extra API call, since `target_thread` is already in hand -- see
    # `_inline_feedback_context`'s docstring. Computed before the reply text
    # below so a static-analyzer source (issue #775) can append the durable-
    # suppression hint to that same reply; `inline_feedback_eligible` already
    # encodes "thread actually resolved and command is a real verdict", which
    # is exactly the gate the hint needs too.
    (
        inline_feedback_eligible,
        inline_source,
        inline_rule_id,
        inline_finding_id,
    ) = _inline_feedback_context(
        _first_comment_body(target_thread), resolved=resolved, command=command
    )

    sha_citation = _sha_citation(commit_sha) if command == "fixed" else ""
    if resolved and pr_approved:
        reply = (
            f"@{actor} marked **F{finding_id}** as `{command}`{sha_citation} and resolved "
            "the thread; all findings are now resolved, so the PR has been approved."
        )
    elif resolved:
        reply = (
            f"@{actor} marked **F{finding_id}** as `{command}`{sha_citation} "
            "and resolved the thread."
        )
        if command == "fixed":
            reply += _FIXED_NO_APPROVE_NOTE
    else:
        reply = (
            f"@{actor} marked **F{finding_id}** as `{command}`{sha_citation}, "
            "but could not resolve the thread; see errors."
        )
    if inline_feedback_eligible and is_analyzer_source(inline_source):
        reply += _ANALYZER_SUPPRESSION_HINT

    return DismissResult(
        reply=reply,
        thread_resolved=resolved,
        review_dismissed=review_dismissed,
        pr_approved=pr_approved,
        feedback_source=inline_source,
        feedback_file=str(target_thread.get("path") or "") if inline_feedback_eligible else "",
        feedback_rule_id=inline_rule_id,
        feedback_finding_id=inline_finding_id,
        feedback_eligible=inline_feedback_eligible,
        acted=bool(resolved or review_dismissed or pr_approved),
        errors=tuple(errors),
    )


def context_from_parent_comment(provider: GitHubProvider, parent_comment_id: int) -> FeedbackContext:
    """Look up FeedbackEntry context from the parent comment of a
    `pull_request_review_comment`-event slash command (the AI finding being
    replied to).

    Mirrors `feedback-command`'s "Extract finding context from parent
    comment" bash step: fetch the comment, validate its author is our bot,
    then parse `source`/`rule_id` from the rendered header via
    `parse_inline_comment_header`. `file` comes from the comment's own
    `path` field (not header parsing) — the header carries no file/line
    location for the inline case (only the body-bullet render does).
    """
    if not parent_comment_id:
        return FeedbackContext(
            missing_reason="review-thread reply has no parent comment (in_reply_to_id is empty)"
        )

    comment = provider.fetch_review_comment(parent_comment_id)
    if comment is None:
        return FeedbackContext(
            missing_reason=f"could not fetch parent comment {parent_comment_id} from GitHub API"
        )

    if comment["login"] != _BOT_LOGIN:
        return FeedbackContext(
            missing_reason=f"parent comment is not from the AI reviewer (author: {comment['login']})"
        )

    parsed = parse_inline_comment_header(comment["body"])
    if not parsed.source:
        # Path is still useful even when the header didn't parse — matches
        # the bash step, which exports file= before exiting on this path.
        return FeedbackContext(
            file=comment["path"],
            missing_reason="could not parse source tag from parent comment header",
        )

    return FeedbackContext(source=parsed.source, file=comment["path"], rule_id=parsed.rule_id)


def resolve_only(
    provider: GitHubProvider,
    parent_comment_id: int,
) -> tuple[bool, tuple[str, ...]]:
    """Resolve the review thread owning `parent_comment_id`, without dismissing
    the owning review under any circumstances.

    Used by `feedback-command`'s "resolve on success" step: `ai-pr-review
    slash` has already persisted the FeedbackEntry and posted a reply by the
    time this runs, so this is a pure best-effort side effect — no reply text,
    no ownership gate (the bash step it replaces resolves the thread
    containing `parent_comment_id` unconditionally, since the slash command
    itself was already validated as posted in reply to one of our comments
    upstream in the workflow's "Validate parent comment is from the bot" gate).

    Returns `(resolved, errors)`. Never raises; every failure mode (transport,
    GraphQL-200-with-errors, thread not found, resolve failure) surfaces as an
    error string for the caller to log, with `resolved=False`.
    """
    errors_before = len(provider._errors)
    threads = provider.fetch_review_threads()
    target_thread = _thread_by_comment_id(threads, parent_comment_id)

    if target_thread is None:
        errors = list(provider._errors[errors_before:])
        errors.append(f"could not locate review thread for parent comment {parent_comment_id}")
        return False, tuple(errors)

    if target_thread.get("isResolved"):
        return True, tuple(provider._errors[errors_before:])

    thread_id = target_thread.get("id")
    if not isinstance(thread_id, str):
        errors = list(provider._errors[errors_before:])
        errors.append(f"review thread for parent comment {parent_comment_id} has no thread id")
        return False, tuple(errors)

    ok, status, body_snippet = provider.resolve_thread(thread_id)
    errors = list(provider._errors[errors_before:])
    if not ok:
        errors.append(f"resolve thread {thread_id}: HTTP {status}: {body_snippet}")
        return False, tuple(errors)
    return True, tuple(errors)


def dismiss_inline_reply(
    provider: GitHubProvider,
    parent_comment_id: int,
    review_id: int | None,
    *,
    actor: str,
    command: str,
    approve_allowed: bool = False,
    commit_sha: str = "",
) -> DismissResult:
    """Handle `/ai-pr-review dismiss|false-positive|wont-fix|fixed` posted as
    a reply to an inline review comment (`pull_request_review_comment` event).

    The review targeted for dismissal is the one owning the resolved thread
    (`pullRequestReview.databaseId` of that thread's first comment) — this
    matches the more precise of the two disagreeing bash copies rather than
    scoping PR-wide.

    `approve_allowed` (issue #590's tighter trust gate, decided by the caller
    from the actor's author association) separately triggers a PR-wide check:
    if this resolution cleared the last active finding across every bot
    CHANGES_REQUESTED review on the PR, a fresh APPROVE review is submitted
    after dismissing the now-clear review(s). This scope is intentionally
    wider than the per-review dismiss above — see `_approve_if_pr_fully_resolved`.
    The caller must pass `approve_allowed=False` unconditionally for
    `command="fixed"` — a fix claim is not a maintainer verdict; see cli.py's
    `dismiss-inline` command, which enforces this override.

    `commit_sha` (only meaningful for `command="fixed"`) is echoed bare in
    the reply so GitHub auto-links it to the commit; it is never validated
    against the repo and plays no role in resolution or dismissal.
    """
    errors_before = len(provider._errors)
    errors: list[str] = []
    threads = provider.fetch_review_threads()
    target_thread = _thread_by_comment_id(threads, parent_comment_id)

    if target_thread is None:
        errors.extend(provider._errors[errors_before:])
        if errors:
            reply = (
                f"@{actor} could not complete the lookup for this comment's "
                "review thread due to an API error; see errors."
            )
        else:
            reply = f"@{actor} could not find the review thread for this comment."
        return DismissResult(reply=reply, errors=tuple(errors))

    body = _first_comment_body(target_thread)
    author = _first_comment_author_login(target_thread) or None
    if not is_owned_by_us(body, author, None, kind="inline"):
        errors.extend(provider._errors[errors_before:])
        return DismissResult(
            reply=f"@{actor} this comment was not posted by this bot; ignoring.",
            errors=tuple(errors),
        )

    thread_id = target_thread.get("id")
    resolved = False
    if not target_thread.get("isResolved") and isinstance(thread_id, str):
        ok, status, body_snippet = provider.resolve_thread(thread_id)
        if ok:
            resolved = True
            # See dismiss_by_finding_id: update in place, no second fetch.
            target_thread["isResolved"] = True
        else:
            errors.append(f"resolve thread {thread_id}: HTTP {status}: {body_snippet}")
    else:
        resolved = bool(target_thread.get("isResolved"))

    review_dismissed = False
    pr_approved = False
    target_review_id = review_id if review_id is not None else _thread_review_id(target_thread)
    if resolved:
        # Verdict recording (best-effort, see _record_verdict): this function
        # doesn't otherwise need the full review list/bodies (dismiss_by_
        # finding_id already fetches them for its own classification, but a
        # thread reply identifies the finding directly). Every inline comment
        # this bot posts embeds its own **[F<n>]** token (assigned to inline
        # and body findings alike), so it's extracted from the comment's own
        # text -- cheaply, with no API call -- rather than needing a separate
        # lookup. Only fetch the review list (needed to locate the canonical
        # review and reverse-lookup the fingerprint) when there's actually an
        # F-id to look up; skipping it otherwise avoids an unnecessary extra
        # API call in the same spirit as the approve_allowed short-circuit
        # right below.
        id_match = _ID_RE.search(body)
        if id_match is not None:
            # Truncate away whatever list_bot_reviews() appends to
            # provider._errors on failure -- this lookup is purely for the
            # best-effort verdict write below and must not surface as a
            # resolve/dismiss error for a concern it has nothing to do with.
            # The truncated entries are still logged (not just discarded) so
            # a genuine access/permission problem with the bot's token isn't
            # invisible to anyone debugging "verdicts never get recorded."
            _verdict_errors_before = len(provider._errors)
            try:
                reviews_for_verdict = provider.list_bot_reviews()
            except httpx.HTTPError as exc:
                # See _record_verdict's docstring: list_bot_reviews() routes
                # through the same retry_transient() that can raise instead
                # of returning a tidy failed response.
                _warn_verdict_failure(f"verdict lookup failed listing reviews: {exc!r}")
                reviews_for_verdict = []
            _truncated_verdict_errors = provider._errors[_verdict_errors_before:]
            del provider._errors[_verdict_errors_before:]
            if _truncated_verdict_errors:
                _warn_verdict_failure(
                    "verdict lookup list_bot_reviews failed (not surfaced as a "
                    "resolve/dismiss error): " + "; ".join(_truncated_verdict_errors)
                )
            bodies_for_verdict = [r.get("body") or "" for r in reviews_for_verdict]
            fingerprint = _fingerprint_for_finding_id(
                bodies_for_verdict, int(id_match.group(1))
            )
            _record_verdict(
                provider,
                reviews_for_verdict,
                fingerprint,
                "fixed" if command == "fixed" else "dismissed",
                resolved_comment_id=_first_comment_id(target_thread),
            )
        # Try the PR-wide approve path FIRST — see dismiss_by_finding_id's
        # identical ordering comment: _approve_if_pr_fully_resolved must run
        # before _dismiss_if_all_resolved, not after, or the per-review
        # dismiss below would already have flipped target_review_id away from
        # CHANGES_REQUESTED by the time the PR-wide check re-lists reviews,
        # making cr_review_ids empty and the approve never fire even in the
        # common single-outstanding-review case.
        if approve_allowed:
            pr_approved, approve_errors = _approve_if_pr_fully_resolved(
                provider,
                threads,
                approve_allowed=approve_allowed,
                dismiss_message="Superseded: all findings resolved via slash command.",
                approve_message=f"@{actor} cleared the last active finding via `/ai-pr-review {command}`.",
            )
            errors.extend(approve_errors)

        if pr_approved:
            review_dismissed = True
        elif target_review_id is not None:
            # Not PR-wide clear (or approve_allowed was False, or the approve
            # attempt raced and dismissed nothing) — fall back to dismissing
            # just this review, the pre-#590 behavior.
            review_dismissed, dismiss_errors = _dismiss_if_all_resolved(
                provider,
                threads,
                target_review_id,
                dismiss_message="Superseded: all findings resolved via slash command.",
            )
            errors.extend(dismiss_errors)

    errors.extend(provider._errors[errors_before:])

    # Full-context feedback entry (issue #769): this function already has the
    # thread's first-comment body and its `path` in hand -- no extra API call
    # needed, unlike `context_from_parent_comment`'s separate
    # fetch_review_comment. See `_inline_feedback_context`'s docstring.
    # Computed before the reply text below so a static-analyzer source
    # (issue #775) can append the durable-suppression hint to that same
    # reply; `inline_feedback_eligible` already encodes "thread actually
    # resolved and command is a real verdict", the same gate the hint needs.
    (
        inline_feedback_eligible,
        inline_source,
        inline_rule_id,
        inline_finding_id,
    ) = _inline_feedback_context(body, resolved=resolved, command=command)

    sha_citation = _sha_citation(commit_sha) if command == "fixed" else ""
    if resolved and pr_approved:
        reply = (
            f"@{actor} marked as `{command}`{sha_citation} and resolved the thread; "
            "all findings are now resolved, so the PR has been approved."
        )
    elif resolved:
        reply = f"@{actor} marked as `{command}`{sha_citation} and resolved the thread."
        if command == "fixed":
            reply += _FIXED_NO_APPROVE_NOTE
    else:
        reply = (
            f"@{actor} marked as `{command}`{sha_citation}, "
            "but could not resolve the thread; see errors."
        )
    if inline_feedback_eligible and is_analyzer_source(inline_source):
        reply += _ANALYZER_SUPPRESSION_HINT

    return DismissResult(
        reply=reply,
        thread_resolved=resolved,
        review_dismissed=review_dismissed,
        pr_approved=pr_approved,
        feedback_source=inline_source,
        feedback_file=str(target_thread.get("path") or "") if inline_feedback_eligible else "",
        feedback_rule_id=inline_rule_id,
        feedback_finding_id=inline_finding_id,
        feedback_eligible=inline_feedback_eligible,
        acted=bool(resolved or review_dismissed or pr_approved),
        errors=tuple(errors),
    )


def persist_verdict(
    result: DismissResult,
    *,
    actor: str,
    command_name: str,
    finding_id: int | None,
    comment_body: str,
    parsed_command: SlashCommand | None,
    enable_feedback_loop: bool,
    feedback_write_allowed: bool,
) -> str:
    """Persist a feedback-store entry for a dismiss/false-positive/wont-fix
    verdict and return the reply text to echo.

    Shared by `dismiss` (top-level, BODY and INLINE F-ids) and `dismiss-inline`
    (issue #769 -- previously the only writer for any inline verdict was the
    now-removed `feedback-command` workflow job's `slash` invocation; this
    makes the dismiss commands the sole owner of both the reply and the store
    write for the whole verdict family, on both event paths).

    `result.feedback_eligible` gates whether there's anything to persist at
    all (False for `fixed` and for UNKNOWN/not-found -- see DismissResult's
    docstring). Two independent knobs then gate the write itself:
    `enable_feedback_loop` (AI_FEEDBACK_LOOP -- the feature is on at all) and
    `feedback_write_allowed` (SLASH_FEEDBACK_WRITE_ALLOWED -- the actor is
    trusted per docs/learning-loop.md's OWNER/MEMBER bar, mirroring the
    existing SLASH_APPROVE_ALLOWED precedent). Three distinct replies so none
    of them lies about what happened.
    """
    if not result.feedback_eligible:
        return result.reply

    if not enable_feedback_loop:
        return f"{result.reply} (feedback loop disabled — not persisted to learning store)"

    if not feedback_write_allowed:
        return (
            f"{result.reply} (not persisted to learning store — recording feedback "
            "requires OWNER or MEMBER association)"
        )

    from ai_pr_review.feedback.store import make_store
    from ai_pr_review.slash.handlers import build_entry
    from ai_pr_review.slash.parser import SlashCommand as _SlashCommand

    command_for_entry = parsed_command or _SlashCommand(
        name=command_name, reason="", raw_body=comment_body, finding_id=finding_id
    )

    class _DismissConfig:
        vcs_provider = "github"

    entry = build_entry(
        command_for_entry,
        source=result.feedback_source,
        file=result.feedback_file,
        rule_id=result.feedback_rule_id,
    )
    stored = make_store(_DismissConfig()).append(entry)
    if stored:
        return result.reply

    _log.warning(
        "dismiss: feedback store failed to persist entry for F%s (command=%r)",
        finding_id,
        command_name,
    )
    finding_ref = f"F{finding_id}" if finding_id is not None else "this finding"
    return (
        f"@{actor} marked **{finding_ref}** as `{command_name}`, but the feedback store "
        "could not persist it (network error or unsupported VCS). "
        "Please retry later or check the workflow logs for details."
    )


def resolve_feedback_context(
    provider: GitHubProvider | None,
    *,
    is_review_comment: bool,
    parent_comment_id: int,
    comment_body: str,
) -> FeedbackContext:
    """Look up source/file/rule_id context for a `feedback-command`
    FeedbackEntry, backing `ai_pr_review.cli`'s `feedback-context` command.

    Two paths, matching the two bash steps this replaces: `is_review_comment`
    looks up context from the parent inline comment being replied to;
    otherwise an F<n> token (accepting the bracketed `[F<n>]` form -- issue
    #735) is extracted from the third whitespace-separated token of the
    comment body's first line (`/ai-pr-review <command> F<n> ...`) and
    resolved against prior review bodies.

    Returns a default (empty) `FeedbackContext` whenever `provider` is
    `None` (provider construction already failed and was reported by the
    caller) or no `F<n>` token is found in the non-review-comment path --
    both silent "not found" cases, matching the two bash steps' own
    `not_found)` branch, which emits nothing at all.
    """
    if provider is None:
        return FeedbackContext()
    if is_review_comment:
        return context_from_parent_comment(provider, parent_comment_id)

    first_line = comment_body.splitlines()[0] if comment_body else ""
    tokens = first_line.split()
    fid_token = tokens[2] if len(tokens) > 2 else ""
    # Accept the bracketed form ("[F1]") shown in review bodies, same as
    # ai_pr_review.slash.parser._FID_RE (issue #735).
    match = re.fullmatch(r"\[?[Ff](\d{1,6})\]?", fid_token)
    if not match:
        return FeedbackContext()
    bodies = bodies_newest_first(provider.list_bot_reviews())
    return context_from_body_finding_id(bodies, int(match.group(1)))
