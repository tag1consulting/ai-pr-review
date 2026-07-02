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
from typing import TYPE_CHECKING, Any

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
class DismissResult:
    """Outcome of a dismiss/false-positive/wont-fix orchestration call."""

    reply: str
    thread_resolved: bool = False
    review_dismissed: bool = False
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
    """Dismiss target_review_id iff none of its own threads remain unresolved.

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

    ok, status, body_snippet = provider.dismiss_review(target_review_id, dismiss_message)
    if not ok:
        errors.append(f"dismiss review {target_review_id}: HTTP {status}: {body_snippet}")
        return False, errors
    return True, errors


def dismiss_by_finding_id(
    provider: GitHubProvider,
    finding_id: int,
    *,
    actor: str,
    command: str,
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
    review_id = _thread_review_id(target_thread)
    if resolved and review_id is not None:
        review_dismissed, dismiss_errors = _dismiss_if_all_resolved(
            provider,
            threads,
            review_id,
            dismiss_message="Superseded: all findings resolved via slash command.",
        )
        errors.extend(dismiss_errors)

    errors.extend(provider._errors[errors_before:])
    if resolved:
        reply = f"@{actor} marked **F{finding_id}** as `{command}` and resolved the thread."
    else:
        reply = f"@{actor} marked **F{finding_id}** as `{command}`, but could not resolve the thread; see errors."
    return DismissResult(
        reply=reply,
        thread_resolved=resolved,
        review_dismissed=review_dismissed,
        errors=tuple(errors),
    )


def dismiss_inline_reply(
    provider: GitHubProvider,
    parent_comment_id: int,
    review_id: int | None,
    *,
    actor: str,
    command: str,
) -> DismissResult:
    """Handle `/ai-pr-review dismiss|false-positive|wont-fix` posted as a
    reply to an inline review comment (`pull_request_review_comment` event).

    The review targeted for dismissal is the one owning the resolved thread
    (`pullRequestReview.databaseId` of that thread's first comment) — this
    matches the more precise of the two disagreeing bash copies rather than
    scoping PR-wide.
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
    target_review_id = review_id if review_id is not None else _thread_review_id(target_thread)
    if resolved and target_review_id is not None:
        review_dismissed, dismiss_errors = _dismiss_if_all_resolved(
            provider,
            threads,
            target_review_id,
            dismiss_message="Superseded: all findings resolved via slash command.",
        )
        errors.extend(dismiss_errors)

    errors.extend(provider._errors[errors_before:])
    if resolved:
        reply = f"@{actor} marked as `{command}` and resolved the thread."
    else:
        reply = f"@{actor} marked as `{command}`, but could not resolve the thread; see errors."
    return DismissResult(
        reply=reply,
        thread_resolved=resolved,
        review_dismissed=review_dismissed,
        errors=tuple(errors),
    )
