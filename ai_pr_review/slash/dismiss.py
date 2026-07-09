"""Slash-command dismiss orchestration — classification, thread resolution,
and review dismissal for `/ai-pr-review dismiss` / `false-positive` / `wont-fix`.

GitHub-only: GitLab and Bitbucket have no F-ID / id-map system.

Ports (and fixes) logic that previously lived only as untested inline bash in
`.github/workflows/slash-commands.yml`. Three bugs were found in that bash
during issue #550's fix (PR #553): a body-scan filter that dropped
out-of-diff-only reviews, a `jq`-fed `while read` loop that couldn't track
multi-line section state, and a source-tag extraction bug. A fourth (#555)
surfaced during live-e2e verification: a `gh api --jq` call returned an HTTP
error body on stdout with exit code 0, defeating the bash null/empty guard.
Moving this logic into Python makes HTTP errors explicit and the classifier
pytest-verifiable instead of live-PR-verifiable only.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from ai_pr_review.vcs._finding_ids import _ID_RE, _LOCATION_RE, _SOURCE_RE
from ai_pr_review.vcs._stale import is_owned_by_us
from ai_pr_review.vcs.marker import extract_id_map

if TYPE_CHECKING:
    from ai_pr_review.vcs.github import GitHubProvider


class FindingLocation(enum.Enum):
    """Which bucket of a review body an F<n> finding lives in."""

    BODY = "body"
    INLINE = "inline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedFinding:
    """Result of classifying a single F<n> token against prior review bodies."""

    location: FindingLocation
    source: str = ""
    file: str = ""
    line: str = ""
    rule_id: str = ""


@dataclass(frozen=True)
class FeedbackContext:
    """Source/file/rule_id context for a `feedback-command` FeedbackEntry.

    Mirrors the two bash extraction steps' combined output contract
    (`source`/`file`/`rule_id`/`context_missing_reason` GITHUB_OUTPUT keys)
    plus their differing severities for a lookup miss:

    - `missing_reason`: a genuine extraction failure (bad/missing parent
      comment, wrong author, unparseable header) — surfaced as a
      `::warning::`, and (parent-comment path only) as `context_missing_reason`
      so the reply step can prepend a transparency note.
    - `notice`: the informational "this is an inline finding, reply to the
      thread instead" hint — surfaced as `::notice::`, never as a warning.
    - Neither set: a plain "not found" (no F<n> token in the comment, or the
      token doesn't match any known finding) — silent, matching bash's
      `not_found)` branch, which emits nothing at all.
    """

    source: str = ""
    file: str = ""
    rule_id: str = ""
    missing_reason: str = ""
    notice: str = ""


@dataclass(frozen=True)
class DismissResult:
    """Outcome of a dismiss/false-positive/wont-fix orchestration call."""

    reply: str
    thread_resolved: bool = False
    review_dismissed: bool = False
    pr_approved: bool = False
    feedback_source: str = ""
    feedback_file: str = ""
    feedback_rule_id: str = ""
    active_body_ids: tuple[int, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)


def _scan_body_bullets(bodies: Sequence[str]) -> dict[int, ClassifiedFinding]:
    """Scan both body-finding buckets in every body, unconditionally.

    Unlike `_parse_existing_ids` (which takes a marker fast-path and never
    scans bullets when the id-map marker is present), this scan always walks
    every body's lines. The id-map marker carries fingerprints for *all*
    finding buckets (inline + both body buckets) indiscriminately, so it
    cannot answer "which bucket is F<n> in" — only a bullet-scan can.

    Section tracking mirrors `_parse_existing_ids`'s fallback loop: entered by
    either the in-diff "### Findings not attached to specific lines" heading
    or the out-of-diff "Out-of-diff analyzer findings" marker (no heading of
    its own), exited by the next "###" or a "</details>" close.
    """
    result: dict[int, ClassifiedFinding] = {}
    for body in bodies:
        in_body_section = False
        for line in body.splitlines():
            stripped = line.strip()
            if "### Findings not attached to specific lines" in stripped:
                in_body_section = True
                continue
            if "Out-of-diff analyzer findings" in stripped:
                in_body_section = True
                continue
            if in_body_section and stripped.startswith("###"):
                in_body_section = False
                continue
            if in_body_section and "</details>" in stripped:
                in_body_section = False
                continue
            if not in_body_section:
                continue
            if not stripped.startswith("- "):
                continue

            id_match = _ID_RE.search(stripped)
            if not id_match:
                continue
            finding_id = int(id_match.group(1))
            if finding_id in result:
                continue

            after_id = stripped[id_match.end() :]
            source = ""
            src_m = _SOURCE_RE.search(after_id)
            if src_m:
                source = src_m.group(1).split(",")[0].strip()

            file_ = ""
            line_no = ""
            loc_m = _LOCATION_RE.search(stripped)
            if loc_m:
                loc_str = loc_m.group(1)
                if ":" in loc_str:
                    file_, _, line_no = loc_str.rpartition(":")
                    if not line_no.isdigit():
                        file_ = loc_str
                        line_no = ""
                else:
                    file_ = loc_str

            result[finding_id] = ClassifiedFinding(
                location=FindingLocation.BODY,
                source=source,
                file=file_,
                line=line_no,
                rule_id=source,
            )
    return result


def classify_finding(bodies: Sequence[str], finding_id: int) -> ClassifiedFinding:
    """Classify F<finding_id> as BODY, INLINE, or UNKNOWN.

    Precedence: an unconditional bullet-scan across both body buckets first
    (BODY); only if that finds nothing, check the id-map's combined values
    across all bodies (INLINE); otherwise UNKNOWN.
    """
    body_findings = _scan_body_bullets(bodies)
    if finding_id in body_findings:
        return body_findings[finding_id]

    for body in bodies:
        if finding_id in extract_id_map(body).values():
            return ClassifiedFinding(location=FindingLocation.INLINE)

    return ClassifiedFinding(location=FindingLocation.UNKNOWN)


def list_active_body_ids(bodies: Sequence[str]) -> list[int]:
    """Return all F<n> IDs currently rendered as body bullets, sorted."""
    return sorted(_scan_body_bullets(bodies).keys())


def _first_comment(thread: dict[str, Any]) -> dict[str, Any]:
    comments = ((thread.get("comments") or {}).get("nodes")) or []
    return comments[0] if comments else {}


def _first_comment_body(thread: dict[str, Any]) -> str:
    return _first_comment(thread).get("body") or ""


def _first_comment_author_login(thread: dict[str, Any]) -> str:
    author = _first_comment(thread).get("author") or {}
    return author.get("login") or ""


def _thread_review_id(thread: dict[str, Any]) -> int | None:
    review = (_first_comment(thread).get("pullRequestReview")) or {}
    rid = review.get("databaseId")
    return int(rid) if isinstance(rid, int) else None


def _thread_by_comment_id(
    threads: Sequence[dict[str, Any]], comment_id: int
) -> dict[str, Any] | None:
    """Find the thread containing a comment with the given REST databaseId.

    Mirrors `dismiss-finding`'s bash correlation
    (`comments.nodes[].databaseId == parent_comment_id`) — a reply-to-a-reply's
    parent may not be the thread's first comment, so all comments in the
    thread are checked, not just `_first_comment`.
    """
    for t in threads:
        comments = ((t.get("comments") or {}).get("nodes")) or []
        if any(c.get("databaseId") == comment_id for c in comments):
            return t
    return None


def _not_found_reply(actor: str, finding_id: int, had_errors: bool) -> str:
    """Reply text for 'could not locate F<n>' — distinguishes a genuine miss
    from a failed lookup (partial/empty data from an errored sub-call), so the
    reply never asserts "not found" when the truth is "couldn't check"."""
    if had_errors:
        return (
            f"@{actor} could not complete the lookup for **F{finding_id}** "
            "due to an API error; see errors."
        )
    return f"@{actor} could not find finding **F{finding_id}** on this pull request."


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
    specifically. This differs from `resolve_stale`/`_dismiss_stale_reviews`,
    which pass `self.config.bot_login` (the REST-style constant,
    "github-actions[bot]") against the same GraphQL-sourced author field —
    per `reference_bot_login_graphql_vs_rest` (unverified this session;
    flagged for live confirmation in story 13-2), GitHub's GraphQL API may
    report the bot's login without the "[bot]" suffix, in which case that
    REST-style comparison would never match and the author check would be a
    silent no-op there too. `None` here is correct under either hypothesis:
    if the logins do differ, `bot_login` would make this dismiss path a
    silent no-op in production; if they don't, `None` only forgoes a narrow
    extra check against a spoofed marker, and a spoofed thread carries the
    attacker's own `pullRequestReview.databaseId`, so at most it could
    trigger dismissal of the attacker's own review, not ours.
    """
    errors: list[str] = []
    unresolved = 0
    for t in threads:
        if t.get("isResolved"):
            continue
        if _thread_review_id(t) != target_review_id:
            continue
        body = _first_comment_body(t)
        author = _first_comment_author_login(t) or None
        if not is_owned_by_us(body, author, None, kind="inline"):
            continue
        unresolved += 1
    if unresolved > 0:
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
    unresolved_by_review: dict[int, int] = {}
    for t in threads:
        if t.get("isResolved"):
            continue
        body = _first_comment_body(t)
        author = _first_comment_author_login(t) or None
        if not is_owned_by_us(body, author, None, kind="inline"):
            continue
        rid = _thread_review_id(t)
        if rid is None:
            continue
        unresolved_by_review[rid] = unresolved_by_review.get(rid, 0) + 1

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
) -> DismissResult:
    """Handle `/ai-pr-review dismiss|false-positive|wont-fix F<n>` from a
    top-level PR comment (no parent review comment to reply to).

    BODY findings: no thread to resolve or review to dismiss (there is no
    single GraphQL thread backing a body-level finding); the caller is
    expected to record a feedback-store entry so the finding is suppressed on
    the next re-run.

    INLINE findings: resolve the thread carrying `**[F<n>]**` gated by our own
    inline marker (never touch another bot's or human's thread), then dismiss
    that thread's owning review if all of its own threads are now resolved.
    When `approve_allowed` (issue #590's tighter trust gate, decided by the
    caller from the actor's author association), also checks whether this
    resolution cleared the *last* active finding PR-wide across all bot
    CHANGES_REQUESTED reviews, and if so submits a fresh APPROVE review.

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
    bodies = [r.get("body") or "" for r in reviews]
    classified = classify_finding(bodies, finding_id)

    if classified.location is FindingLocation.UNKNOWN:
        errors.extend(provider._errors[errors_before:])
        return DismissResult(
            reply=_not_found_reply(actor, finding_id, had_errors=bool(errors)),
            errors=tuple(errors),
        )

    if classified.location is FindingLocation.BODY:
        errors.extend(provider._errors[errors_before:])
        return DismissResult(
            reply=(
                f"@{actor} marked **F{finding_id}** as `{command}`. "
                "This finding will be suppressed on future review runs."
            ),
            feedback_source=classified.source,
            feedback_file=classified.file,
            feedback_rule_id=classified.rule_id,
            active_body_ids=tuple(list_active_body_ids(bodies)),
            errors=tuple(errors),
        )

    # INLINE: find the thread carrying this F-id token, gated by our own
    # inline marker (never touch another bot's or human's thread).
    threads = provider.fetch_review_threads()
    target_thread: dict[str, Any] | None = None
    for t in threads:
        body = _first_comment_body(t)
        author = _first_comment_author_login(t) or None
        if not is_owned_by_us(body, author, None, kind="inline"):
            continue
        if f"[F{finding_id}]" not in body:
            continue
        target_thread = t
        break

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
    if resolved and pr_approved:
        reply = (
            f"@{actor} marked **F{finding_id}** as `{command}` and resolved the thread; "
            "all findings are now resolved, so the PR has been approved."
        )
    elif resolved:
        reply = f"@{actor} marked **F{finding_id}** as `{command}` and resolved the thread."
    else:
        reply = f"@{actor} marked **F{finding_id}** as `{command}`, but could not resolve the thread; see errors."
    return DismissResult(
        reply=reply,
        thread_resolved=resolved,
        review_dismissed=review_dismissed,
        pr_approved=pr_approved,
        errors=tuple(errors),
    )


def parse_inline_comment_header(body: str) -> ClassifiedFinding:
    """Parse the rendered header of a single inline review comment.

    Mirrors `_build_inline_comment_body`'s render:
    ``{icon} **[{severity}]**{id_token} {tag} {text}``. After stripping the
    optional ``**[F<n>]**`` id token, the first bracket group is severity and
    the second is the source tag — unlike a body bullet (``- {icon} **[F{n}]**
    [{source}] {text}``, no leading severity bracket), so `_SOURCE_RE`'s
    first-match behavior used by `_scan_body_bullets` does not apply here.
    For multi-source findings (e.g. ``[code-reviewer, security-reviewer]``),
    only the first source tag is kept, matching the body-level convention.
    Matches `_scan_body_bullets`'s existing rule_id convention: for a SARIF
    source (e.g. ``sarif:bandit``), `rule_id` is the full source string, not
    a separately-rendered bracket — no render path emits a distinct third
    bracket group for the rule ID.

    Returns `ClassifiedFinding(location=UNKNOWN)` (empty source) if no
    source tag could be parsed, so callers can distinguish a parse failure
    from a genuine empty source.
    """
    first_line = body.splitlines()[0] if body else ""
    stripped = _ID_RE.sub("", first_line, count=1).strip()
    brackets = _SOURCE_RE.findall(stripped)
    if len(brackets) < 2:
        return ClassifiedFinding(location=FindingLocation.UNKNOWN)

    source = brackets[1].split(",")[0].strip()
    rule_id = source if source.startswith("sarif:") else ""
    return ClassifiedFinding(location=FindingLocation.INLINE, source=source, rule_id=rule_id)


_BOT_LOGIN: Final[str] = "github-actions[bot]"


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


def context_from_body_finding_id(bodies: Sequence[str], finding_id: int) -> FeedbackContext:
    """Look up FeedbackEntry context from an F<n> token in an `issue_comment`
    (top-level PR comment) slash command.

    Mirrors `feedback-command`'s "Extract finding context from review body"
    bash+Python-heredoc step, built on the same `classify_finding` used by
    `dismiss_by_finding_id`. Matches bash's three-way severity split: BODY
    populates context, INLINE sets `notice` only (bash's `inline)` branch
    emits an advisory `::notice::`, never a warning), and UNKNOWN
    (bash's `not_found)` branch) returns an all-empty context — silent,
    not even a notice.
    """
    classified = classify_finding(bodies, finding_id)
    if classified.location is FindingLocation.BODY:
        return FeedbackContext(source=classified.source, file=classified.file, rule_id=classified.rule_id)
    if classified.location is FindingLocation.INLINE:
        return FeedbackContext(
            notice=(
                f"F{finding_id} is an inline finding; for full context in the feedback "
                "entry, reply directly to the finding thread instead of using a top-level comment"
            )
        )
    return FeedbackContext()


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
) -> DismissResult:
    """Handle `/ai-pr-review dismiss|false-positive|wont-fix` posted as a
    reply to an inline review comment (`pull_request_review_comment` event).

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
    if resolved and pr_approved:
        reply = (
            f"@{actor} marked as `{command}` and resolved the thread; "
            "all findings are now resolved, so the PR has been approved."
        )
    elif resolved:
        reply = f"@{actor} marked as `{command}` and resolved the thread."
    else:
        reply = f"@{actor} marked as `{command}`, but could not resolve the thread; see errors."
    return DismissResult(
        reply=reply,
        thread_resolved=resolved,
        review_dismissed=review_dismissed,
        pr_approved=pr_approved,
        errors=tuple(errors),
    )
