"""GitHub slash-command F-ID/body classification — F-ID classification, reply-
text templates, and the thin CLI-adjacent helpers backing
`/ai-pr-review dismiss` / `dismiss-inline` / `false-positive` / `wont-fix` /
`fixed`, plus `feedback-context` / `resolve-thread`. Renamed from `dismiss.py`
in #833: the module had grown past that name well before this rename.

Split in #849: this module now holds only the pure classification logic (no
I/O) plus the CLI-adjacent helpers that need no HTTP mocking to test
(`tests/python/slash/test_github_orchestration.py` /
`test_github_orchestration_cli_helpers.py`). Everything that needs network
I/O moved to `ai_pr_review.slash.github_ops`: `dismiss_by_finding_id`,
`dismiss_inline_reply`, `resolve_only`, `context_from_parent_comment`, and
`_record_verdict` make a live GitHub API call; `persist_verdict` and
`resolve_feedback_context` need a separate feedback-store call instead --
neither takes a `provider` argument or calls a `GitHubProvider` method,
`persist_verdict` writes through `feedback/store.py`'s `GitBranchStore`. All
of the above, plus their private helpers, moved together since they share
the same "needs network I/O" boundary even though the I/O differs.
`github_ops.py`'s own docstring lays out the full reasoning (including why
that module stayed under `ai_pr_review.slash` rather than `ai_pr_review.vcs`,
despite its tests living in `tests/python/vcs/`). This module has no
dependency on `github_ops.py`; the dependency runs one way, `github_ops.py`
-> this module.

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
import os as _os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from ai_pr_review.vcs._body import TRUNCATION_MARKER
from ai_pr_review.vcs._finding_ids import (
    _ID_RE,
    _LOCATION_RE,
    _SOURCE_RE,
    BODY_SECTION_START_MARKERS,
    _ends_body_section,
    _pick_primary_source,
    fingerprint_for_finding_id,
    safe_review_id,
)
from ai_pr_review.vcs.marker import extract_id_map, extract_inline_meta, extract_judge_map

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
class InlineFeedbackContext:
    """Result of `_inline_feedback_context` -- an INLINE finding's
    feedback-store context, resolved from its own comment body.

    A named dataclass rather than a positional tuple specifically because
    `corroborated: bool` and `confidence: int | None` sit adjacent to each
    other: `bool` is a subtype of `int` in Python's type system, so a
    positional swap between them would type-check cleanly and only surface
    as bad data in the feedback store. Matches this file's existing
    convention (`ClassifiedFinding`, `FeedbackContext`, `DismissResult`) for
    exactly this reason.
    """

    eligible: bool
    source: str
    rule_id: str
    finding_id: int | None
    judge_verdict: str | None
    corroborated: bool
    confidence: int | None


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
    # The F<n> this verdict is for, when known. Threaded into the feedback-
    # store entry's dedup key (see feedback/store.py's _dedup_key) -- without
    # it, dismissing two DIFFERENT findings that share command/source/file
    # within the dedup window would collide on the same key and the second
    # one would be silently dropped as a false-positive "duplicate". Always
    # known on the top-level (dismiss_by_finding_id) path, since the caller
    # supplies finding_id directly; on the inline-reply
    # (dismiss_inline_reply) path it comes from the `**[F<n>]**` token this
    # bot embeds in every inline comment it posts (present for effectively
    # every real finding since F-ids were extended to inline findings), so
    # this is empty only for a legacy inline comment posted before that.
    feedback_finding_id: int | None = None
    # True only for a dismiss/false-positive/wont-fix verdict that actually
    # took effect (BODY or INLINE) -- never for `fixed` (not a maintainer
    # verdict on validity) or UNKNOWN (nothing to record). This is the caller's
    # single signal for whether to persist a feedback-store entry -- see
    # github_ops.py's `persist_verdict`. Kept distinct from
    # feedback_source/feedback_file being non-empty because those are also
    # (ab)used as BODY-vs-INLINE classification signals elsewhere; this field
    # exists so that distinction never has to double as "should we write".
    feedback_eligible: bool = False
    # Judge-pass state for the feedback-store entry's `extras` (judge-verdict
    # instrumentation, #841): read back from the inline comment's own
    # metadata marker (`vcs.marker.extract_inline_meta`) for an INLINE
    # finding, or from the review body's sibling judge-map marker
    # (`vcs.marker.extract_judge_map`, via `_judge_data_for_finding_id`) for a
    # BODY finding -- either way, since this CLI process has no access to the
    # original in-memory `Finding`. Default (None/False/None) when the
    # fingerprint can't be resolved, the finding predates this feature, or it
    # never went through the judge pass at all.
    feedback_judge_verdict: str | None = None
    feedback_corroborated: bool = False
    feedback_confidence: int | None = None
    # True whenever this call resolved/dismissed/recorded/approved something
    # real -- drives the done/confused reaction. Explicit rather than derived
    # from the fields above (issue #769): a successful BODY `fixed` sets none
    # of feedback_source/feedback_file/thread_resolved/review_dismissed/
    # pr_approved (there's no thread to resolve for a body-level finding), so
    # a derived expression reacted "confused" to a command that fully
    # succeeded.
    acted: bool = False
    active_body_ids: tuple[int, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)


def _scan_body_bullets_one(body: str) -> dict[int, ClassifiedFinding]:
    """Scan a single review body's two body-finding buckets for `**[F<n>]**`
    bullets.

    Section tracking uses the same `_ends_body_section` exit check as
    `_parse_existing_ids`'s fallback loop (`vcs/_finding_ids.py`): entered by
    any of `BODY_SECTION_START_MARKERS` (the in-diff "### Findings not
    attached to specific lines" heading, the out-of-diff "Out-of-diff
    analyzer findings" marker with no heading of its own, or the APPROVE-path
    "### Findings (informational)" heading — issue #645), exited by the next
    "###" heading, a "</details>" close, or the token-usage marker (#758).
    """
    result: dict[int, ClassifiedFinding] = {}
    in_body_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in BODY_SECTION_START_MARKERS):
            in_body_section = True
            continue
        if in_body_section and _ends_body_section(stripped):
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
            source = _pick_primary_source(src_m.group(1))

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


def bodies_newest_first(reviews: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return review bodies ordered newest (highest id) first.

    `classify_finding` and `list_active_body_ids` both need this ordering --
    see their docstrings for why. Every production call site
    (`dismiss_by_finding_id`, `ai_pr_review.cli`'s `dismiss` and
    `feedback-context` commands) builds its `bodies` list from
    `provider.list_bot_reviews()`/`_list_prior_bot_reviews()`, which return
    reviews in GitHub API order (oldest first) -- relying on that order
    implicitly would silently break classification if a future caller ever
    passed reviews in a different order, so every caller should route
    through this helper instead of building `bodies` by hand.

    Uses `ai_pr_review.vcs._finding_ids.safe_review_id` (shared with
    `ai_pr_review.vcs._canonical.select_canonical`/`merge_verdicts`) so this
    module's notion of "highest id" can never independently drift from
    theirs.
    """
    ordered = sorted(reviews, key=safe_review_id, reverse=True)
    return [str(r.get("body") or "") for r in ordered]


def classify_finding(bodies: Sequence[str], finding_id: int) -> ClassifiedFinding:
    """Classify F<finding_id> as BODY, INLINE, or UNKNOWN.

    `bodies` must be ordered newest-first (see `bodies_newest_first`).
    Classification walks bodies from newest to oldest and stops at the
    first body that renders F<finding_id> at all: a body bullet (BODY,
    using that body's own source/file/line context) takes precedence over
    an id-map entry (INLINE) *within the same body*, but a newer body's
    rendering always takes precedence over an older body's, regardless of
    which bucket each puts it in. Only when no body at all knows the ID
    (dropped from the review entirely) does this return UNKNOWN.

    F-IDs are stable across review cycles, but a finding can move buckets
    between runs: an out-of-diff Low bullet in one cycle can become an
    inline High finding with its own thread in the next, once its line
    enters the diff (or the reverse, once the line leaves the diff again).
    Scanning bodies oldest-first (this function's original behavior) could
    return BODY for a finding that has since moved inline in the newest
    review, which sends `/ai-pr-review dismiss F<n>` down the body-only
    branch (feedback-store entry, no thread fetch, no resolve, no review
    dismissal) even though the finding is now blocking the PR via an open,
    unresolved inline thread. Confirmed as the root cause of a live
    incident: a body-level dismiss on a finding that had moved inline never
    resolved the thread, and the blocking review was never dismissed.

    An id-map hit in a *truncated* body (carrying `TRUNCATION_MARKER`,
    `ai_pr_review.vcs._body.truncate_body`'s trailer) is not trusted as a
    definitive INLINE verdict on its own: `GitHubProvider.
    _finalize_body_with_markers` truncates the visible findings text first
    and appends the id-map marker afterward, so a truncated body can list an
    ID in its id-map while the bullet that would have classified it BODY was
    cut off. Such a hit is remembered as a fallback and only used if no
    older body resolves the ID more definitively (a bullet, or an id-map hit
    in a body that isn't truncated); otherwise a >64KB review would
    misclassify its own overflowed body findings as INLINE and send their
    dismiss down the wrong branch, the same failure shape as the
    oldest-first bug this function was rewritten to fix.
    """
    inline_fallback: ClassifiedFinding | None = None
    for body in bodies:
        bullets = _scan_body_bullets_one(body)
        if finding_id in bullets:
            return bullets[finding_id]
        if finding_id in extract_id_map(body).values():
            if TRUNCATION_MARKER in body:
                if inline_fallback is None:
                    inline_fallback = ClassifiedFinding(location=FindingLocation.INLINE)
                continue
            return ClassifiedFinding(location=FindingLocation.INLINE)
    if inline_fallback is not None:
        return inline_fallback
    return ClassifiedFinding(location=FindingLocation.UNKNOWN)


def list_active_body_ids(bodies: Sequence[str]) -> list[int]:
    """Return every F<n> ID currently classified as a body-level finding,
    sorted.

    `bodies` must be ordered newest-first (see `bodies_newest_first`).
    Backs the "please specify a finding ID" reply (`ai_pr_review.cli`'s
    `dismiss` command with no `F<n>` given), so the IDs offered there must
    agree with what `/ai-pr-review dismiss F<n>` will actually do for each
    one -- delegates to `classify_finding` itself (the single source of
    truth for the newest-first precedence rule) rather than re-deriving a
    similar-but-possibly-drifted rule. An ID that has since moved inline in
    the newest review is correctly excluded even though an older review
    still renders it as a body bullet.
    """
    candidate_ids: set[int] = set()
    for body in bodies:
        candidate_ids.update(_scan_body_bullets_one(body).keys())
        candidate_ids.update(extract_id_map(body).values())
    return sorted(
        fid
        for fid in candidate_ids
        if classify_finding(bodies, fid).location is FindingLocation.BODY
    )


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


# "fixed" reply wording — see SlashCommand.is_feedback_command's docstring for
# why "fixed" is neither a feedback-store verdict nor auto-approving, unlike
# dismiss/false-positive/wont-fix.
_FIXED_NO_APPROVE_NOTE = (
    " The review will re-run on the new commit, so findings are not "
    "auto-approved for a `fixed` claim."
)

# Issue #775: a verdict (dismiss/false-positive/wont-fix) on a static-analyzer
# finding is scoped to that exact fingerprint (file + line + code) -- it can
# never stop the same analyzer pattern from recurring in a different file,
# since analyzer findings never pass through an LLM-agent prompt and the
# learning-loop store is advisory-only for agents (feedback/inject.py). The
# only durable suppression mechanism for analyzer findings is a repo-level
# suppressions.json rule. Appended only when `is_analyzer_source` matches the
# finding's source -- LLM-agent findings already benefit from the soft
# suppression the learning-loop's <repo-feedback> injection provides, so this
# note would be misleading there and is deliberately left off.
_ANALYZER_SUPPRESSION_HINT = (
    " This dismissal applies to this exact occurrence only. If this analyzer "
    "keeps flagging the same pattern in other files, add a rule to "
    "`.github/ai-pr-review/suppressions.json` (see docs/suppression.md)."
)


def _sha_citation(commit_sha: str) -> str:
    """Render " in <sha>" for the reply, or "" if no SHA was supplied.

    Emitted bare (no backticks/code-span) so GitHub auto-links the SHA to its
    commit. The SHA is never validated against the repo -- an unknown or
    mistyped SHA simply renders as plain text; it plays no role in thread
    resolution or dismissal, both of which are driven by the finding ID or
    comment ID alone.
    """
    return f" in {commit_sha}" if commit_sha else ""


def parse_inline_comment_header(body: str) -> ClassifiedFinding:
    """Parse the rendered header of a single inline review comment.

    Mirrors `_build_inline_comment_body`'s render:
    ``{icon} **[{severity}]**{id_token} {tag} {text}``. After stripping the
    optional ``**[F<n>]**`` id token, the first bracket group is severity and
    the second is the source tag — unlike a body bullet (``- {icon} **[F{n}]**
    [{source}] {text}``, no leading severity bracket), so `_SOURCE_RE`'s
    first-match behavior used by `_scan_body_bullets_one` does not apply here.
    For multi-source findings (e.g. ``[code-reviewer, security-reviewer]``),
    `_pick_primary_source` picks a static-analyzer name over an LLM agent's
    when both are present (issue #776), matching `_scan_body_bullets_one`'s
    identical convention. Matches `_scan_body_bullets_one`'s existing rule_id
    convention too: for a SARIF source (e.g. ``sarif:bandit``), `rule_id` is
    the full source string, not a separately-rendered bracket — no render
    path emits a distinct third bracket group for the rule ID.

    Returns `ClassifiedFinding(location=UNKNOWN)` (empty source) if no
    source tag could be parsed, so callers can distinguish a parse failure
    from a genuine empty source.
    """
    first_line = body.splitlines()[0] if body else ""
    stripped = _ID_RE.sub("", first_line, count=1).strip()
    brackets = _SOURCE_RE.findall(stripped)
    if len(brackets) < 2:
        return ClassifiedFinding(location=FindingLocation.UNKNOWN)

    source = _pick_primary_source(brackets[1])
    rule_id = source if source.startswith("sarif:") else ""
    return ClassifiedFinding(location=FindingLocation.INLINE, source=source, rule_id=rule_id)


def _judge_data_for_finding_id(
    bodies: Sequence[str], finding_id: int
) -> tuple[str | None, bool, int | None]:
    """Recover a BODY-level finding's judge-pass state (judge-verdict
    instrumentation, #841), or (None, False, None) if unavailable.

    Mirrors `_scan_body_bullets_one`'s reverse-lookup pattern: resolve the
    fingerprint first, then scan `bodies` (already newest-first) for the
    first judge-map marker (`vcs.marker.extract_judge_map`) carrying an
    entry for that fingerprint. Returns the defaults whenever the fingerprint
    can't be resolved, no body carries a judge-map entry for it (the finding
    predates this feature, or never went through the judge pass), or the
    entry fails validation inside `extract_judge_map` itself.
    """
    fp = fingerprint_for_finding_id(bodies, finding_id)
    if fp is None:
        return None, False, None
    for body in bodies:
        entry = extract_judge_map(body).get(fp)
        if entry is not None:
            jv = entry.get("jv")
            conf = entry.get("conf")
            return (
                jv if isinstance(jv, str) else None,
                entry.get("corr") is True,
                conf if isinstance(conf, int) else None,
            )
    return None, False, None


def _inline_feedback_context(
    body: str, *, resolved: bool, command: str
) -> InlineFeedbackContext:
    """Compute an `InlineFeedbackContext` for an INLINE finding's
    feedback-store entry.

    Shared by `dismiss_by_finding_id`'s INLINE branch (top-level comment
    naming an inline F<n>) and `dismiss_inline_reply` (reply on the inline
    thread itself) -- both had near-identical logic here before this was
    extracted. `file` isn't returned: it's a trivial `target_thread.get(
    "path")` each caller already has in hand and reads directly.

    Eligible whenever the thread actually resolved and the command is a real
    verdict (never `fixed` -- not a maintainer verdict on validity), even if
    the header fails to parse a source tag -- `file` alone is still useful
    context to the caller, matching `context_from_parent_comment`'s same
    fallback.

    `finding_id` comes from the `**[F<n>]**` token this bot embeds in every
    inline comment it posts (issue #769's dedup-key fix: without a real id
    here, two DIFFERENT findings dismissed within the feedback store's dedup
    window would collide on the same key and the second would be silently
    dropped -- see `feedback/store.py`'s `_dedup_key`).

    `judge_verdict`/`corroborated`/`confidence` (judge-verdict instrumentation):
    read from the comment's own metadata marker via
    `ai_pr_review.vcs.marker.extract_inline_meta`, the same marker
    `classified`/`finding_id` above are recovered from -- no extra API call.
    All three fall back to `None`/`False`/`None` when the marker is absent,
    predates this feature, or the finding never went through the judge pass.
    """
    if not (resolved and command != "fixed"):
        return InlineFeedbackContext(
            eligible=False, source="", rule_id="", finding_id=None,
            judge_verdict=None, corroborated=False, confidence=None,
        )
    classified = parse_inline_comment_header(body)
    finding_id_match = _ID_RE.search(body)
    finding_id = int(finding_id_match.group(1)) if finding_id_match is not None else None
    meta = extract_inline_meta(body)
    judge_verdict = meta.judge_verdict if meta is not None else None
    corroborated = meta.corroborated if meta is not None else False
    confidence = meta.confidence if meta is not None else None
    return InlineFeedbackContext(
        eligible=True,
        source=classified.source,
        rule_id=classified.rule_id,
        finding_id=finding_id,
        judge_verdict=judge_verdict,
        corroborated=corroborated,
        confidence=confidence,
    )


_BOT_LOGIN: Final[str] = "github-actions[bot]"


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


# ---------------------------------------------------------------------------
# CLI-adjacent helpers (issue #825)
#
# Moved out of ai_pr_review/cli.py so the `dismiss`, `dismiss-inline`,
# `feedback-context`, and `resolve-thread` Click commands are left as thin
# option-parsing wrappers: parse/validate CLI-level concerns (options, env
# vars, exit codes), then delegate. These four (GitHubProviderError,
# resolve_github_provider, no_finding_id_reply, dismiss_failure_annotation)
# need no HTTP mocking to test -- they format text or resolve config, never
# call a GitHubProvider's HTTP/GraphQL methods themselves. The two CLI
# helpers that DO need network I/O (persist_verdict, resolve_feedback_context)
# moved to ai_pr_review.slash.github_ops in #849 alongside the rest of that
# module's I/O-needing code -- persist_verdict's I/O is a feedback-store
# call (feedback/store.py's GitBranchStore), not a GitHubProvider method;
# resolve_feedback_context does call a GitHubProvider method.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GitHubProviderError:
    """Non-fatal GitHub-provider construction failure.

    Carries the exact ``"<command_label>: ..."`` message text that both of
    `resolve_github_provider`'s callers in `ai_pr_review.cli`
    (`_build_github_provider_or_exit`, `_build_github_provider_or_none`) echo
    to stderr before diverging on how to fail: `sys.exit(1)` for the
    GitHub-only `dismiss`/`dismiss-inline` commands, versus `return None` for
    the best-effort `feedback-context`/`resolve-thread` commands. The message
    text is identical either way -- only the failure mode differs -- so this
    dataclass carries the one shared piece of state and the CLI layer alone
    decides what to do about it.
    """

    message: str

def resolve_github_provider(command_label: str) -> GitHubProvider | GitHubProviderError:
    """Build a `GitHubProvider` from env, or return a `GitHubProviderError`
    describing why.

    `command_label` names the calling CLI subcommand (e.g. "dismiss",
    "feedback-context") for the error message prefix.

    `VCS_PROVIDER` is checked *before* calling `provider_from_env()` — that
    dispatcher eagerly reads provider-specific env vars and raises its own
    errors first, so an `isinstance` check after the call would fire too
    late under a non-GitHub `VCS_PROVIDER`.
    """
    from ai_pr_review.vcs import GitHubProvider, ProviderConfigError, provider_from_env

    vcs = (_os.environ.get("VCS_PROVIDER") or "github").strip().lower()
    if vcs != "github":
        return GitHubProviderError(
            f"{command_label}: ai-pr-review {command_label} is GitHub-only (VCS_PROVIDER={vcs!r})"
        )

    try:
        provider = provider_from_env()
    except ProviderConfigError as exc:
        return GitHubProviderError(f"{command_label}: {exc}")

    if not isinstance(provider, GitHubProvider):
        # Unreachable given the vcs-name gate above; narrows the type for
        # mypy and guards against provider_from_env's dispatch logic changing.
        return GitHubProviderError(f"{command_label}: expected a GitHub provider")

    return provider

def no_finding_id_reply(actor: str, command_name: str, active_ids: Sequence[int]) -> str:
    """Reply for `/ai-pr-review dismiss|false-positive|wont-fix|fixed` posted
    with no `F<n>` given, backing `ai_pr_review.cli`'s `dismiss` command.

    `active_ids` must already be sorted (see `list_active_body_ids`).
    """
    if active_ids:
        ids_text = ", ".join(f"F{n}" for n in active_ids)
        return (
            f"@{actor} please specify a finding ID, e.g. `/ai-pr-review {command_name} F{active_ids[0]}`. "
            f"Active findings: {ids_text}."
        )
    return f"@{actor} there are no active body-level findings to {command_name}."

def dismiss_failure_annotation(
    command_label: str, errors: tuple[str, ...], *, thread_resolved: bool = False
) -> str | None:
    """Build the GitHub Actions ``::error::`` annotation line for a dismiss/
    wont-fix/false-positive command that hit a VCS API error (#611), or
    `None` when nothing should be emitted.

    Backs `ai_pr_review.cli`'s `_emit_dismiss_failure_annotation`, which
    echoes the returned line to stderr (the one remaining CLI-specific bit);
    this function decides only whether/what to say.

    The calling workflow step always exits 0 on this path (see the ``dismiss``/
    ``dismiss-inline`` docstrings: "not found" and "API error" both count as
    "handled", not "command failure") so the reply can still post via
    ``actions-token`` even when the error came from a different, failing
    token (``github-token``, the PAT). That means the job's own conclusion
    stays green regardless -- this annotation is the only signal that the
    underlying dismiss did not actually happen, surfaced on the PR's Checks
    tab rather than only in the run log. Matches the pattern
    ``emit_post_failure_annotation`` (#588/#618) uses for the sibling
    review-posting path. Deliberately omits the raw error strings (already
    logged as ``::warning::`` by the caller) to avoid duplicating any
    credential fragment into the more widely-visible annotation.

    ``thread_resolved`` distinguishes two distinct failure shapes: the finding
    itself may still be genuinely unresolved (thread resolution failed), or the
    thread may have resolved fine while a secondary step -- dismissing the
    stale review, or the PR-wide auto-approve -- errored afterward. Asserting
    "NOT dismissed/resolved" in the second case would contradict the CLI's own
    reply text (which correctly says the thread was resolved) and mislead
    anyone reading the Checks tab into re-running a command that already
    succeeded.

    Only emitted when running in GitHub Actions (``GITHUB_ACTIONS=true``) and
    when *errors* is non-empty.
    """
    if _os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    if not errors:
        return None
    outcome = (
        "the thread was resolved, but a follow-up step (review dismissal or "
        "PR approval) failed."
        if thread_resolved
        else "the finding was likely NOT dismissed/resolved."
    )
    return (
        f"::error::ai-pr-review {command_label}: the command could not complete "
        f"due to {len(errors)} API error(s); see the ::warning:: lines above "
        f"for detail. {outcome}"
    )
