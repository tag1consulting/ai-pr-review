"""GitHub VCS provider — ports post-review.sh.

Implements the VcsProvider protocol for GitHub REST + GraphQL. All stale
cleanup is marker-gated (closes #183, #184); cleanup runs only after a
successful post (2.FR-10).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import GITHUB_MAX_BODY_SIZE, compute_headline, truncate_body
from ai_pr_review.vcs._inline import (
    is_inline_eligible,
    is_suggestion_range_valid,
    is_suggestion_safe,
    partition_findings,
    split_body_findings,
)
from ai_pr_review.vcs._stale import graphql_bot_login, is_owned_by_us
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
    ID_MAP_MARKER_PREFIX,
    INLINE_MARKER,
    SUMMARY_MARKER_PREFIX,
    append_inline_marker,
    append_skip_marker,
    build_summary_marker,
    extract_summary_sha,
    has_skip_marker,
    replace_summary_sha,
)
from ai_pr_review.vcs.protocol import (
    DiffContext,
    FindingsResult,
    PostEvent,
    StaleResult,
    SummaryResult,
)

if TYPE_CHECKING:
    from ai_pr_review.vcs._canonical import CanonicalReview, Classified, PriorThread

_log = logging.getLogger(__name__)

_BOT_LOGIN_DEFAULT: Final[str] = "github-actions[bot]"

_GRAPHQL_PATH: Final[str] = "/graphql"


def _blob_link(*, owner: str, repo: str, head_sha: str, file: str, line: int | None) -> str:
    """Build a GitHub blob-permalink URL, optionally anchored to a line.

    Used only for body-level findings, which have no diff-line anchor of
    their own for a reviewer to click through to (unlike an inline review
    comment, which GitHub already anchors natively). The documented
    diff-view anchor format requires a comment ID that doesn't exist for a
    body-level finding (confirmed live: an existing inline comment's
    `html_url` is `.../pull/{n}#discussion_r{comment_id}`, not a path-hash
    scheme) -- the blob permalink is the only always-constructible target.

    `Finding.file` carries no documented "always repo-relative" contract,
    and at least one static analyzer has been observed emitting an absolute
    container path (e.g. `/workspace/ai_pr_review/vcs/github.py`) instead --
    a leading "/" would otherwise survive into `quoted_path` as an empty
    first segment, producing a double-slash URL. Stripped here defensively;
    the resulting link may still point at the wrong location for a
    genuinely absolute path (that's the analyzer's bug to fix, tracked
    separately), but it is at least never malformed.
    """
    quoted_path = "/".join(quote(seg) for seg in file.lstrip("/").split("/"))
    url = f"https://github.com/{owner}/{repo}/blob/{head_sha}/{quoted_path}"
    if line is not None:
        url += f"#L{line}"
    return url


def _parse_next_link(link_header: str) -> str | None:
    """Extract the rel=next URL from a GitHub Link response header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segs = part.strip().split(";")
        if len(segs) < 2:
            continue
        url = segs[0].strip().lstrip("<").rstrip(">")
        rels = [s.strip() for s in segs[1:]]
        if 'rel="next"' in rels:
            return url
    return None


def _safe_int(value: object, default: int = 0) -> int:
    """Convert value to int, returning default on ValueError/TypeError.

    Logs a warning when a non-None value cannot be converted, so unexpected
    API payloads (schema changes, malformed responses) are visible in logs.
    """
    try:
        if isinstance(value, (int, float, str, bytes)):
            return int(value)
    except (ValueError, TypeError):
        pass
    if value is not None:
        _log.warning("_safe_int: unexpected non-integer review ID %r; skipping", value)
    return default


@dataclass(frozen=True)
class GitHubConfig:
    owner: str
    repo: str
    pr_number: int
    token: str
    bot_login: str = _BOT_LOGIN_DEFAULT
    base_url: str = "https://api.github.com"
    # Kill switch for canonical-review reuse (issue tracker: PR #716). When
    # False, post_findings always POSTs a fresh review exactly like every
    # release before this feature -- an escape hatch for a consumer pinned
    # to @main who hits a bug in the reuse path without waiting for a
    # version pin or a revert. Set via AI_CANONICAL_REUSE=false.
    canonical_reuse: bool = True


def build_client(config: GitHubConfig, retry: RetryPolicy | None = None) -> RecordingClient:
    """Build a RecordingClient preconfigured for GitHub API calls."""
    http = httpx.Client(
        base_url=config.base_url,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )
    return RecordingClient(
        http=http,
        recorder=TapeRecorder.from_env(provider="github"),
        retry_policy=retry or RetryPolicy(),
    )


@dataclass
class GitHubProvider:
    """GitHub REST + GraphQL implementation of VcsProvider."""

    config: GitHubConfig
    client: RecordingClient
    _errors: list[str] = field(default_factory=list, init=False, repr=False)
    # Thread ids post_findings PATCHed (update/escalate) or successfully
    # reopened (recurred) during its most recent call, in this same process.
    # resolve_stale consults this so it doesn't immediately re-resolve a
    # thread canonical-review reuse just deliberately kept alive -- see
    # issue #718. Reset at the top of every post_findings call, not
    # accumulated across calls.
    _kept_alive_thread_ids: set[str] = field(
        default_factory=set, init=False, repr=False
    )

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def _issue_comments_url(self) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/issues/{c.pr_number}/comments"

    def _issue_comment_url(self, comment_id: int) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/issues/comments/{comment_id}"

    def _reviews_url(self) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/pulls/{c.pr_number}/reviews"

    def _dismiss_url(self, review_id: int) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/pulls/{c.pr_number}/reviews/{review_id}/dismissals"

    def _review_url(self, review_id: int) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/pulls/{c.pr_number}/reviews/{review_id}"

    def _review_comment_url(self, comment_id: int) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/pulls/comments/{comment_id}"

    def _review_comment_reply_url(self, comment_id: int) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/pulls/{c.pr_number}/comments/{comment_id}/replies"

    def _pull_request_url(self) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/pulls/{c.pr_number}"

    def _check_runs_url(self) -> str:
        c = self.config
        return f"/repos/{c.owner}/{c.repo}/check-runs"

    # ------------------------------------------------------------------
    # Summary comment find helpers
    # ------------------------------------------------------------------
    def _list_summary_comments(self) -> list[dict[str, Any]]:
        """Return all issue comments containing the summary marker prefix."""
        results: list[dict[str, Any]] = []
        url: str | None = self._issue_comments_url()
        params: dict[str, Any] | None = {"per_page": 100}
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                self._errors.append(
                    f"list_summary_comments: HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return results
            page = resp.json() or []
            for item in page:
                body = item.get("body") or ""
                if SUMMARY_MARKER_PREFIX in body:
                    results.append(item)
            # httpx returns the next link via headers
            next_url = _parse_next_link(resp.headers.get("link", ""))
            url = next_url
            params = None  # params are embedded in the next link URL
        return results

    # ------------------------------------------------------------------
    # Prior bot reviews — id/state/body reconstruction for ID-map assembly
    # and canonical-review-reuse classification
    # ------------------------------------------------------------------
    def _list_prior_bot_reviews(self) -> list[dict[str, Any]]:
        """Return `{"id", "state", "body"}` for every prior bot review,
        paginated. On any HTTP error mid-pagination, returns `[]` rather
        than partial results (the same #550/#553 guarantee the now-removed
        `_list_prior_bot_review_bodies` always had) -- distinct from
        `list_bot_reviews`, which appends to `self._errors` and keeps
        whatever it collected so far; that method has its own existing
        callers (`_record_verdict`) with that partial-result contract
        already baked in, so this is a separate method rather than a change
        to it (see `select_canonical`'s docstring for the resulting
        write/read discrepancy this leaves open).

        Feeds both the body-finding ID-map assembler in `post_findings`
        (filtering for `ID_MAP_MARKER_PREFIX`/`"**[F"` inline, replacing the
        old dedicated method) and the canonical-review-reuse classification
        path (`ai_pr_review.vcs._canonical.select_canonical`/
        `merge_verdicts`, which need every prior review's id/state/body, not
        just the id-map-bearing subset).
        """
        c = self.config
        reviews: list[dict[str, Any]] = []
        url: str | None = self._reviews_url()
        params: dict[str, Any] | None = {"per_page": 100}
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                _log.warning(
                    "github: could not list reviews: HTTP %d", resp.status_code
                )
                return []
            for review in resp.json() or []:
                if (review.get("user") or {}).get("login") != c.bot_login:
                    continue
                if review.get("state") not in (
                    "CHANGES_REQUESTED", "COMMENTED", "APPROVED", "DISMISSED"
                ):
                    continue
                rid = review.get("id")
                if not isinstance(rid, int):
                    continue
                reviews.append(
                    {"id": rid, "state": review.get("state") or "", "body": review.get("body") or ""}
                )
            url = _parse_next_link(resp.headers.get("link", ""))
            params = None
        return reviews

    # ------------------------------------------------------------------
    # get_last_reviewed_sha
    # ------------------------------------------------------------------
    def get_last_reviewed_sha(self) -> str | None:
        comments = self._list_summary_comments()
        if not comments:
            return None
        # The bash engine takes `last` (most recent); GitHub returns in created order
        # ascending by default, so the last entry is the most recent.
        latest = comments[-1]
        return extract_summary_sha(
            latest.get("body") or "",
            context_hint=f"issue_comment#{latest.get('id')}",
        )

    def get_summary_body(self) -> str | None:
        comments = self._list_summary_comments()
        if not comments:
            return None
        return comments[-1].get("body") or None

    # ------------------------------------------------------------------
    # post_summary — upsert the single summary comment keyed by marker
    # ------------------------------------------------------------------
    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        if not summary_body.strip():
            return SummaryResult(
                comment_id=None, created=False, updated=False, error="empty summary body"
            )

        marker = build_summary_marker(head_sha)
        truncated = truncate_body(summary_body)
        body = (
            f"{marker}\n{truncated}\n\n---\n"
            "*AI Review Summary — generated by "
            "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
        )

        existing = self._list_summary_comments()
        if existing:
            keep = existing[0]
            keep_id = int(keep["id"])
            resp = self.client.request(
                "PATCH", self._issue_comment_url(keep_id), json_body={"body": body}
            )
            if resp.status_code >= 400:
                err = f"update summary: HTTP {resp.status_code}: {resp.text[:200]}"
                self._errors.append(err)
                return SummaryResult(
                    comment_id=keep_id, created=False, updated=False, error=err
                )
            # Delete any duplicate summary comments (cosmetic, non-fatal)
            for dup in existing[1:]:
                dup_id = int(dup["id"])
                self.client.request("DELETE", self._issue_comment_url(dup_id))
            return SummaryResult(comment_id=keep_id, created=False, updated=True)

        resp = self.client.request(
            "POST", self._issue_comments_url(), json_body={"body": body}
        )
        if resp.status_code >= 400:
            err = f"create summary: HTTP {resp.status_code}: {resp.text[:200]}"
            self._errors.append(err)
            return SummaryResult(comment_id=None, created=False, updated=False, error=err)
        data = resp.json() or {}
        new_id = int(data.get("id", 0)) or None
        return SummaryResult(comment_id=new_id, created=True, updated=False)

    # ------------------------------------------------------------------
    # _list_skip_comments — find existing skip comments by SKIP_MARKER
    # ------------------------------------------------------------------
    def _list_skip_comments(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        url: str | None = self._issue_comments_url()
        params: dict[str, Any] | None = {"per_page": 100}
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                self._errors.append(
                    f"list_skip_comments: HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return results
            page = resp.json() or []
            for item in page:
                body = item.get("body") or ""
                if has_skip_marker(body):
                    results.append(item)
            next_url = _parse_next_link(resp.headers.get("link", ""))
            url = next_url
            params = None
        return results

    # ------------------------------------------------------------------
    # post_skip_comment — upsert skip comment (mirrors post_summary)
    # ------------------------------------------------------------------
    def post_skip_comment(self, reason: str) -> SummaryResult:
        body = append_skip_marker(
            f"**AI Review skipped.** {reason.strip() or 'No changes to review.'}"
        )
        existing = self._list_skip_comments()
        if existing:
            keep = existing[0]
            keep_id = int(keep["id"])
            resp = self.client.request(
                "PATCH", self._issue_comment_url(keep_id), json_body={"body": body}
            )
            if resp.status_code >= 400:
                err = f"update skip comment: HTTP {resp.status_code}: {resp.text[:200]}"
                self._errors.append(err)
                return SummaryResult(
                    comment_id=keep_id, created=False, updated=False, error=err
                )
            for dup in existing[1:]:
                dup_id = int(dup["id"])
                self.client.request("DELETE", self._issue_comment_url(dup_id))
            return SummaryResult(comment_id=keep_id, created=False, updated=True)

        resp = self.client.request(
            "POST", self._issue_comments_url(), json_body={"body": body}
        )
        if resp.status_code >= 400:
            err = f"skip comment: HTTP {resp.status_code}: {resp.text[:200]}"
            self._errors.append(err)
            return SummaryResult(comment_id=None, created=False, updated=False, error=err)
        data = resp.json() or {}
        new_id = int(data.get("id", 0)) or None
        return SummaryResult(comment_id=new_id, created=True, updated=False)

    # ------------------------------------------------------------------
    # advance_sha_watermark — patches the existing summary comment's marker
    # ------------------------------------------------------------------
    def advance_sha_watermark(self, new_sha: str) -> bool:
        """Rewrite the sha= field in the existing summary marker. Returns True if
        a summary comment was found and patched successfully."""
        existing = self._list_summary_comments()
        if not existing:
            return False
        # Pick the OLDEST marker-bearing comment (existing[0]) so this is
        # consistent with post_summary, which keeps existing[0] and deletes
        # the rest. GitLab and Bitbucket use existing[0] in both code paths;
        # this aligns GitHub with that convention.
        keep = existing[0]
        keep_id = int(keep["id"])
        old_body = keep.get("body") or ""
        new_body = replace_summary_sha(
            old_body, new_sha, context_hint=f"issue_comment#{keep_id}"
        )
        if new_body == old_body:
            return False
        resp = self.client.request(
            "PATCH", self._issue_comment_url(keep_id), json_body={"body": new_body}
        )
        if resp.status_code >= 400:
            self._errors.append(
                f"advance_sha: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # post_findings — pull-request review with inline comments + fallbacks
    # ------------------------------------------------------------------
    def post_findings(
        self,
        findings: Sequence[Finding],
        diff: DiffContext,
        *,
        event: PostEvent,
        failed_agents: Sequence[str] = (),
        token_table: str = "",
        agent_prompt: str = "",
        max_inline: int = 25,
        enable_suggestions: bool = True,
    ) -> FindingsResult:
        from ai_pr_review.diff.linemap import parse_diff_sets
        from ai_pr_review.vcs._body import format_body_finding, join_findings
        from ai_pr_review.vcs._canonical import (
            classify,
            decide_action,
            dedupe_thread_claims,
            merge_verdicts,
            parse_prior_thread,
            select_canonical,
        )
        from ai_pr_review.vcs._finding_ids import (
            assemble_id_map,
            fingerprint,
            known_fingerprints,
        )

        # Reset, not accumulate: this set describes only the most recent
        # post_findings call, which is what the very next resolve_stale call
        # (orchestrate.py runs them back-to-back) needs to know about.
        self._kept_alive_thread_ids = set()

        _added, _new_file = parse_diff_sets(diff.diff_text)
        eligible_new = {(lr.file, lr.line) for lr in _added}
        eligible_ctx = {(lr.file, lr.line) for lr in _new_file}

        # Fetch prior reviews once, feeding both the existing body-finding
        # ID-map assembler (unchanged) and canonical-review classification
        # (new). Fetched unconditionally -- NOT gated on `if findings:` as
        # the ID-map fetch alone used to be -- because canonical-review
        # selection matters just as much on a fully clean run (zero
        # findings, event APPROVE): that's exactly the "quiet rerun" case
        # this feature exists to stop from posting a brand-new APPROVE
        # review every single cycle. Fail-soft on any error: an empty
        # `reviews`/`all_threads` makes every finding classify "new" and
        # `select_canonical` return `None`, which is exactly today's
        # pre-canonical-reuse behavior (always POST a fresh review).
        try:
            reviews = self._list_prior_bot_reviews()
        except Exception as exc:  # noqa: BLE001
            import os as _os
            # Emit a GitHub Actions ::warning:: annotation only when running
            # inside GitHub Actions to avoid polluting local/test output.
            if _os.environ.get("GITHUB_ACTIONS") == "true":
                print(
                    f"::warning::ai-pr-review: failed to fetch prior reviews; "
                    f"body-finding IDs and cross-run classification may not "
                    f"be stable this cycle: {exc}",
                    flush=True,
                )
            _log.warning(
                "github: failed to fetch prior reviews; body-finding IDs may "
                "not be stable and every finding will classify as new: %s", exc,
            )
            reviews = []
        prior_bodies = [
            r["body"] for r in reviews
            if ID_MAP_MARKER_PREFIX in r["body"] or "**[F" in r["body"]
        ]
        id_map = assemble_id_map(prior_bodies, list(findings))
        prior_known_fps = known_fingerprints(prior_bodies)

        canonical: CanonicalReview | None
        verdicts: dict[str, str]
        all_threads: list[PriorThread]
        threads_fetch_complete: bool
        if self.config.canonical_reuse:
            canonical = select_canonical(reviews)
            verdicts = merge_verdicts(reviews)

            # fetch_review_threads() never raises on an HTTP/GraphQL error --
            # it appends to self._errors and returns whatever it collected so
            # far (possibly []), so completeness has to be checked via
            # self._errors, not a try/except. threads_fetch_complete gates
            # the dismiss-superseded-review step below: an incomplete fetch
            # must never be treated as "zero unresolved threads".
            errors_before_threads = len(self._errors)
            try:
                thread_nodes = self.fetch_review_threads()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "github: failed to fetch review threads for classification; "
                    "every finding will classify as new this cycle: %s", exc,
                )
                thread_nodes = []
                self._errors.append(f"fetch_review_threads: {exc}")
            threads_fetch_complete = len(self._errors) == errors_before_threads
            all_threads = []
            for node in thread_nodes:
                # graphql_bot_login() strips the REST-style "[bot]" suffix:
                # GitHub's GraphQL API reports the bot's login as
                # "github-actions" (no suffix) -- verified live against a
                # real thread -- while self.config.bot_login is the
                # REST-style "github-actions[bot]" constant. Comparing the
                # raw constant here would make is_owned_by_us() reject every
                # real thread, leaving all_threads permanently empty in
                # production (#717); passing bot_login=None instead would
                # fix that but drop the author check as a defense-in-depth
                # signal entirely -- normalizing is strictly better now that
                # the exact format difference is confirmed, not hypothesized.
                parsed = parse_prior_thread(
                    node,
                    bot_login=graphql_bot_login(self.config.bot_login),
                    id_map_bodies=prior_bodies,
                )
                if parsed is not None:
                    all_threads.append(parsed)
        else:
            # Kill switch (AI_CANONICAL_REUSE=false): behave exactly like
            # every release before this feature existed -- no canonical to
            # reuse, no verdicts to suppress/recur against, no threads to
            # match. Every finding classifies "new" below and this run
            # always POSTs a fresh review.
            canonical = None
            verdicts = {}
            all_threads = []
            threads_fetch_complete = True

        classified = dedupe_thread_claims(
            [classify(f, verdicts=verdicts, all_threads=all_threads) for f in findings]
        )

        # Apply side effects for update/escalate/recurred-with-thread.
        # suppressed produces no side effect (never reposted, permanently).
        # recurred-without-thread (body-level) produces no side effect here
        # either -- it's rendered in the body below instead of the original
        # comment it no longer has.
        inline_updated = 0
        replies_posted = 0
        suppressed_count = 0
        recurred_body_fps: set[str] = set()
        updated_verdicts = dict(verdicts)
        for c in classified:
            if c.kind == "suppressed":
                suppressed_count += 1
            elif c.kind == "recurred":
                updated_verdicts[fingerprint(c.finding)] = "recurred"
                if c.thread is not None:
                    reopened = self._notify_recurrence(c.thread, diff.head_sha)
                    replies_posted += 1
                    if reopened:
                        # Keep all_threads in sync with the reopen this just
                        # performed on GitHub: the dismiss-superseded-review
                        # gate below (_has_unresolved_owned_threads) reads
                        # this same list, and without this update it would
                        # see the stale is_resolved=True snapshot fetched
                        # before this side effect ran -- letting a review
                        # with a thread this run just reopened be dismissed
                        # as if it had no unresolved threads at all.
                        reopened_thread = c.thread
                        all_threads = [
                            dataclasses.replace(t, is_resolved=False)
                            if t.thread_id == reopened_thread.thread_id
                            else t
                            for t in all_threads
                        ]
                        # Only now (post-reopen) does this thread show up as
                        # unresolved to resolve_stale's own GraphQL fetch --
                        # record it so that fetch doesn't immediately
                        # re-resolve the thread this run just reopened (#718).
                        self._kept_alive_thread_ids.add(reopened_thread.thread_id)
                else:
                    recurred_body_fps.add(fingerprint(c.finding))
            elif c.kind in ("update", "escalate") and c.thread is not None:
                # This thread corresponds to a still-active finding whether
                # or not the cosmetic PATCH below succeeds -- resolve_stale
                # must never resolve it out from under an open finding (#718).
                self._kept_alive_thread_ids.add(c.thread.thread_id)
                patched = self._apply_thread_update(
                    c, enable_suggestions=enable_suggestions
                )
                if patched:
                    inline_updated += 1
                    # Only claim an escalation happened if the underlying
                    # PATCH actually landed -- otherwise the reply would
                    # assert "severity escalated" for a comment that still
                    # shows the old severity.
                    if c.kind == "escalate":
                        self._notify_escalation(c.thread, c.finding, diff.head_sha)
                        replies_posted += 1

        # Only genuinely-new findings, plus body-level recurrences (which have
        # no existing comment to reply on), go through the normal render
        # pipeline below. Everything else either already has its own
        # up-to-date comment (update/escalate/recurred-with-thread) or is
        # permanently suppressed.
        render_findings = [
            c.finding
            for c in classified
            if c.kind == "new" or (c.kind == "recurred" and c.thread is None)
        ]

        # partition_findings/_build_inline_comment_payload run before
        # decide_action (not after, as in the original draft) so
        # any_new_inline_eligible reflects which findings actually landed an
        # inline comment, not just which were eligible in principle. Raw
        # eligibility (is_inline_eligible) also counts a finding that
        # `max_inline` bumped to the body, or whose payload build failed, as
        # if it got an inline slot -- which would force a fresh POST for a
        # PR simply over the inline cap even when nothing about it actually
        # changed (issue #719).
        inline_candidates, body_findings = partition_findings(
            render_findings, eligible_new=eligible_new, max_inline=max_inline
        )
        inline_comments: list[dict[str, Any]] = []
        actually_inline_fps: set[str] = set()
        for f in inline_candidates:
            payload = _build_inline_comment_payload(
                f,
                eligible_new=eligible_new,
                eligible_context=eligible_ctx,
                enable_suggestions=enable_suggestions,
                finding_id=id_map.get(fingerprint(f)),
            )
            if payload is not None:
                inline_comments.append(payload)
                actually_inline_fps.add(fingerprint(f))
            else:
                body_findings.append(f)

        any_new_inline_eligible = any(
            c.kind == "new" and fingerprint(c.finding) in actually_inline_fps
            for c in classified
        )
        action = decide_action(
            canonical,
            event=event,
            classified=classified,
            any_new_inline_eligible=any_new_inline_eligible,
            known_fingerprints=prior_known_fps,
        )
        _log.info(
            "github: canonical-review decision=%s (canonical=%s, canonical_state=%s, "
            "event=%s, new=%d, any_new_inline_eligible=%s, "
            "new_high_or_critical=%s)",
            action,
            canonical.review_id if canonical is not None else None,
            canonical.state if canonical is not None else None,
            event,
            sum(1 for c in classified if c.kind == "new"),
            any_new_inline_eligible,
            any(
                c.kind == "new" and c.finding.severity in ("Critical", "High")
                for c in classified
            ),
        )

        in_diff_body, ood_body = split_body_findings(body_findings)
        body_bullets: list[str] = []
        ood_bullets: list[str] = []
        for f in in_diff_body:
            loc_note = ""
            if f.file and f.line is not None and (f.file, f.line) not in eligible_new:
                loc_note = " *(line not in diff)*"
            if fingerprint(f) in recurred_body_fps:
                loc_note += " *(recurred)*"
            if f.file:
                url = _blob_link(
                    owner=self.config.owner, repo=self.config.repo,
                    head_sha=diff.head_sha, file=f.file, line=f.line,
                )
                loc_note += f" [↗]({url})"
            body_bullets.append(format_body_finding(
                f,
                location_note=loc_note,
                finding_id=id_map.get(fingerprint(f)),
            ))
        for f in ood_body:
            loc_note = ""
            if f.file and f.line is not None and (f.file, f.line) not in eligible_new:
                loc_note = " *(line not in diff)*"
            if fingerprint(f) in recurred_body_fps:
                loc_note += " *(recurred)*"
            if f.file:
                url = _blob_link(
                    owner=self.config.owner, repo=self.config.repo,
                    head_sha=diff.head_sha, file=f.file, line=f.line,
                )
                loc_note += f" [↗]({url})"
            ood_bullets.append(format_body_finding(
                f,
                location_note=loc_note,
                finding_id=id_map.get(fingerprint(f)),
            ))

        # The headline must describe the PR's current state, not just this
        # run's diff: fold in every still-active classified finding
        # (update/escalate/recurred-with-thread keep their own comment, just
        # not re-rendered in the body list above) plus any open owned thread
        # this run didn't touch at all.
        headline_findings = list(render_findings)
        headline_findings.extend(
            c.finding for c in classified if c.kind in ("update", "escalate")
        )
        headline_findings.extend(
            c.finding for c in classified
            if c.kind == "recurred" and c.thread is not None
        )
        touched_thread_ids = {
            c.thread.thread_id for c in classified if c.thread is not None
        }
        headline_findings.extend(
            _carried_forward_finding(t)
            for t in all_threads
            if not t.is_resolved and t.thread_id not in touched_thread_ids
        )
        headline_inline_count = len(inline_comments) + sum(
            1 for c in classified if c.kind in ("update", "escalate")
        )

        body = _render_review_body(
            event=event,
            findings=headline_findings,
            inline_count=headline_inline_count,
            body_findings_text=join_findings(body_bullets),
            out_of_diff_findings_text=join_findings(ood_bullets),
            failed_agents=failed_agents,
            token_table=token_table,
            agent_prompt=agent_prompt,
        )
        body = self._finalize_body_with_markers(
            body, id_map=id_map, verdicts=updated_verdicts
        )

        if action == "put" and canonical is not None:
            put_ok, _put_status, put_snippet, put_skipped = self._try_put_canonical(
                canonical, body, diff
            )
            if put_ok:
                return FindingsResult(
                    review_id=canonical.review_id,
                    inline_posted=0,
                    body_findings=len(body_bullets),
                    event=event,
                    inline_updated=inline_updated,
                    suppressed=suppressed_count,
                    replies_posted=replies_posted,
                    reused_review=True,
                    skipped=put_skipped,
                )
            _log.warning(
                "github: could not reuse canonical review %d (%s); "
                "falling back to posting a fresh review",
                canonical.review_id, put_snippet,
            )

        # action == "post", or the PUT attempt above was abandoned.
        result = self._post_new_review(
            body=body,
            inline_comments=inline_comments,
            event=event,
            diff=diff,
            body_bullets_count=len(body_bullets),
        )

        # Dismiss the superseded canonical review only *after* confirming the
        # replacement actually landed as a real REQUEST_CHANGES review --
        # not before. Dismissing first (the original ordering here) mirrors
        # the exact hazard `_dismiss_stale_reviews` is already careful to
        # avoid: if `_post_new_review`'s own fallback chain degrades to a
        # COMMENT review or a plain issue comment, dismissing the old CR
        # beforehand would leave the PR with no blocking review at all,
        # silently unblocking something that should still be blocked.
        # `threads_fetch_complete` additionally guards against the fail-open
        # case where `fetch_review_threads()` degraded to a partial/empty
        # list on an HTTP or GraphQL error: an incomplete fetch must never
        # be treated as "zero unresolved threads".
        if (
            canonical is not None
            and canonical.state == "CHANGES_REQUESTED"
            and event == "REQUEST_CHANGES"
            and result.event == "REQUEST_CHANGES"
            and not result.degraded_to_comment
            and not result.error
            and threads_fetch_complete
            and not self._has_unresolved_owned_threads(canonical.review_id, all_threads)
        ):
            ok, status, snippet = self.dismiss_review(
                canonical.review_id, "Superseded by a subsequent review run."
            )
            if not ok:
                _log.warning(
                    "github: failed to dismiss superseded review %d: HTTP %d",
                    canonical.review_id, status,
                )
                self._errors.append(
                    f"dismiss superseded review {canonical.review_id}: "
                    f"HTTP {status}: {snippet}"
                )

        return FindingsResult(
            review_id=result.review_id,
            inline_posted=result.inline_posted,
            body_findings=result.body_findings,
            event=result.event,
            degraded_to_comment=result.degraded_to_comment,
            error=result.error,
            inline_updated=inline_updated,
            suppressed=suppressed_count,
            replies_posted=replies_posted,
            reused_review=False,
        )

    def _post_new_review(
        self,
        *,
        body: str,
        inline_comments: list[dict[str, Any]],
        event: PostEvent,
        diff: DiffContext,
        body_bullets_count: int,
    ) -> FindingsResult:
        """POST a brand-new review: GitHub's APPROVE+inline-comments
        workaround, then the three-tier degrade path (COMMENT retry, then a
        plain issue comment). Extracted verbatim from the pre-canonical-reuse
        `post_findings` so this always-available fallback keeps its
        existing, well-tested behavior unchanged; `post_findings` calls this
        only when `decide_action()` actually warrants a fresh review object,
        or when reusing the canonical review failed/was abandoned.
        """
        original_inline_comments: list[dict[str, Any]] = []
        # GitHub disallows inline comments on an APPROVE review. When approving
        # with inline findings, post them as COMMENT first then APPROVE body-only.
        if event == "APPROVE" and inline_comments:
            pre_payload = {
                "body": body,
                "event": "COMMENT",
                "commit_id": diff.head_sha,
                "comments": inline_comments,
            }
            resp_pre = self.client.request(
                "POST", self._reviews_url(), json_body=pre_payload
            )
            if resp_pre.status_code >= 400:
                self._errors.append(
                    f"pre-APPROVE COMMENT: HTTP {resp_pre.status_code}: "
                    f"{resp_pre.text[:200]}"
                )
                inline_posted_count = 0
            else:
                inline_posted_count = len(inline_comments)
            # Keep a copy for the fallback path — fallback needs the original
            # inline findings even after inline_comments is cleared below.
            original_inline_comments = inline_comments
            inline_comments = []
        else:
            inline_posted_count = 0

        review_payload: dict[str, Any] = {
            "body": body,
            "event": event,
            "commit_id": diff.head_sha,
            "comments": inline_comments,
        }

        resp = self.client.request("POST", self._reviews_url(), json_body=review_payload)
        if resp.status_code < 400:
            data = resp.json() or {}
            return FindingsResult(
                review_id=int(data.get("id", 0)) or None,
                inline_posted=inline_posted_count + len(inline_comments),
                body_findings=body_bullets_count,
                event=event,
                degraded_to_comment=False,
            )

        # Fallback 1: retry as COMMENT (GITHUB_TOKEN may not be able to block/approve)
        degrade_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
        self._errors.append(f"post review ({event}): {degrade_detail}")
        if event in ("APPROVE", "REQUEST_CHANGES"):
            import os as _os
            _log.warning(
                "github: post review as %s failed (%s); retrying as COMMENT — "
                "the PR will NOT actually be %s",
                event, degrade_detail,
                "approved" if event == "APPROVE" else "marked as changes requested",
            )
            if _os.environ.get("GITHUB_ACTIONS") == "true":
                print(
                    f"::warning::ai-pr-review: could not post review as {event} "
                    f"({degrade_detail}); falling back to a plain COMMENT review. "
                    "This PR has NOT been approved by ai-pr-review.",
                    flush=True,
                )
            if event == "APPROVE":
                # The body was rendered for an APPROVE outcome and claims the
                # PR was approved. Since GitHub is about to receive this as a
                # COMMENT instead, that claim would be false — prepend a
                # visible correction so nobody mistakes the comment for a
                # real approval. The raw HTTP detail stays in the log/annotation
                # above rather than the public body: it's upstream response
                # text of unknown shape, and embedding it here risks breaking
                # the blockquote (embedded newline) or widening its audience
                # from log readers to anyone with PR read access.
                note = (
                    "> **Note:** GitHub rejected posting this review as an "
                    "approval; posting the findings below as a comment "
                    "instead. **This PR has NOT been approved by "
                    "ai-pr-review.** See the workflow run log for details.\n\n"
                )
                # body may already sit near GITHUB_MAX_BODY_SIZE (truncated
                # earlier in this method for the original event) — reserve
                # room for the note so the COMMENT retry can't itself fail
                # for being too long.
                note_bytes = len(note.encode("utf-8"))
                body = truncate_body(
                    body, limit=max(0, GITHUB_MAX_BODY_SIZE - note_bytes)
                )
                body = note + body
            review_payload["event"] = "COMMENT"
            review_payload["body"] = body
            resp2 = self.client.request(
                "POST", self._reviews_url(), json_body=review_payload
            )
            if resp2.status_code < 400:
                data = resp2.json() or {}
                return FindingsResult(
                    review_id=int(data.get("id", 0)) or None,
                    inline_posted=inline_posted_count + len(inline_comments),
                    body_findings=body_bullets_count,
                    event="COMMENT",
                    degraded_to_comment=True,
                )
            self._errors.append(
                f"retry as COMMENT: HTTP {resp2.status_code}: {resp2.text[:200]}"
            )

        # Fallback 2: plain PR issue comment (loses inline anchoring).
        # original_inline_comments is initialized at method entry so it is always
        # defined here — APPROVE path saves the pre-clear copy; other paths leave it [].
        fallback_inline = original_inline_comments or inline_comments
        fallback = self._render_fallback_body(body, fallback_inline)
        resp3 = self.client.request(
            "POST", self._issue_comments_url(), json_body={"body": fallback}
        )
        if resp3.status_code < 400:
            return FindingsResult(
                review_id=None,
                inline_posted=0,
                body_findings=body_bullets_count + len(fallback_inline),
                event="COMMENT",
                degraded_to_comment=True,
            )

        err = f"All three posting attempts failed; last HTTP {resp3.status_code}"
        self._errors.append(err)
        return FindingsResult(
            review_id=None,
            inline_posted=0,
            body_findings=0,
            event=event,
            degraded_to_comment=True,
            error=err,
        )

    def _finalize_body_with_markers(
        self, body: str, *, id_map: dict[str, int], verdicts: dict[str, str]
    ) -> str:
        """Truncate `body` to GitHub's limit, then append the inline-ownership
        marker plus (space permitting) the id-map and verdicts markers.

        Reserves room for ALL THREE markers before truncating the visible
        body -- fixes a pre-existing bug where only the id-map marker's size
        was reserved and the inline-ownership marker (`INLINE_MARKER`,
        always appended) was added *after* truncation, letting the final
        body exceed `GITHUB_MAX_BODY_SIZE` by `len(INLINE_MARKER)` bytes. If
        the id-map and verdicts markers together don't fit, the id-map
        marker is dropped first (it's reconstructible from prior review
        bodies via the fallback bullet-scan in `_finding_ids.py`); the
        verdicts marker -- durable human dismiss/fixed decisions, not
        reconstructible from anything else -- is kept even if it still
        doesn't fit, with a loud warning rather than a silent drop.
        """
        from ai_pr_review.vcs.marker import build_id_map_marker, build_verdicts_marker

        id_map_marker = ""
        try:
            id_map_marker = build_id_map_marker(id_map)
        except Exception as exc:  # noqa: BLE001
            _log.warning("github: failed to build id-map marker: %s", exc)

        verdicts_marker = ""
        if verdicts:
            try:
                verdicts_marker = build_verdicts_marker(verdicts)
            except Exception as exc:  # noqa: BLE001
                _log.warning("github: failed to build verdicts marker: %s", exc)

        _MIN_BODY_BYTES = 4096
        inline_reserve = len(INLINE_MARKER.encode("utf-8")) + 1
        id_map_reserve = len(id_map_marker.encode("utf-8")) + 1 if id_map_marker else 0
        verdicts_reserve = (
            len(verdicts_marker.encode("utf-8")) + 1 if verdicts_marker else 0
        )
        reserve = inline_reserve + id_map_reserve + verdicts_reserve

        if id_map_marker and reserve > GITHUB_MAX_BODY_SIZE - _MIN_BODY_BYTES:
            _log.warning(
                "github: id-map + verdicts markers (%d bytes) too large to fit in "
                "review body for %s/%s PR #%s; dropping id-map marker for this "
                "cycle — ID stability may degrade",
                reserve, self.config.owner, self.config.repo, self.config.pr_number,
            )
            id_map_marker = ""
            id_map_reserve = 0
            reserve = inline_reserve + verdicts_reserve

        if verdicts_marker and reserve > GITHUB_MAX_BODY_SIZE - _MIN_BODY_BYTES:
            _log.warning(
                "github: verdicts marker (%d bytes) alone still too large to fit "
                "for %s/%s PR #%s; keeping it anyway — human dismiss/fixed "
                "decisions must not be silently lost, even if the provider then "
                "rejects the oversized body instead",
                verdicts_reserve, self.config.owner, self.config.repo,
                self.config.pr_number,
            )

        # Floor at 0 as a defensive guard; reserve is always <= GITHUB_MAX_BODY_SIZE
        # in the ordinary case, but clamp to prevent any edge case producing a
        # negative limit.
        truncate_limit = max(0, GITHUB_MAX_BODY_SIZE - reserve)
        body = truncate_body(body, limit=truncate_limit)
        body = append_inline_marker(body)
        if id_map_marker:
            body += "\n" + id_map_marker
        if verdicts_marker:
            body += "\n" + verdicts_marker
        return body

    def _try_put_canonical(
        self, canonical: CanonicalReview, body: str, diff: DiffContext
    ) -> tuple[bool, int, str, bool]:
        """Pre-write concurrency re-check, then PUT the canonical review's
        body. Returns (ok, status, snippet, skipped).

        `ok=False` means: something else wrote to the canonical review since
        it was selected (a concurrent run or slash command) -- the caller
        must fall through to POSTing a fresh review rather than clobbering a
        concurrent write. This narrows the lost-update window from "the
        whole run" to "one re-check to one write"; it does not eliminate the
        race (no distributed lock exists) -- see docs/features.md for the
        `concurrency:` group recommendation for consumers that push rapidly.

        `skipped=True` (only possible alongside `ok=True`) means: the PR's
        head has already moved past this run's diff, so a newer run already
        owns the canonical -- no write happened at all, distinct from a
        write that actually landed. The caller must not treat this the same
        as a real post for watermark-advance or stale-cleanup purposes: this
        run's `diff.head_sha` is stale, and advancing the watermark to it
        after a newer run has already advanced it to a later SHA would
        regress the incremental-diff baseline backward.
        """
        recheck = self.get_review_state_and_body(canonical.review_id)
        if recheck is None:
            return False, 0, "could not re-fetch canonical review before write", False
        state, current_body = recheck
        if state != canonical.state or current_body != canonical.body:
            return (
                False, 0,
                "canonical review changed since selection (concurrent write)",
                False,
            )

        head_sha = self.get_pr_head_sha()
        if head_sha is not None and head_sha != diff.head_sha:
            _log.info(
                "github: PR head has advanced past this run's diff (%s != %s); "
                "skipping canonical write, a newer run owns it",
                diff.head_sha, head_sha,
            )
            return True, 200, "skipped: PR head advanced past this run's diff", True

        ok, status, snippet = self.update_review_body(canonical.review_id, body)
        return ok, status, snippet, False

    def _has_unresolved_owned_threads(
        self, review_id: int, all_threads: Sequence[PriorThread]
    ) -> bool:
        """Does `review_id` still have at least one unresolved thread we own?

        Mirrors the exact rule `_dismiss_stale_reviews` already applies
        (`unresolved_by_review.get(rid, 0) > 0`): a `CHANGES_REQUESTED`
        review must never be dismissed while it still has open findings, or
        the slash-command PR-wide auto-approve check
        (`ai_pr_review.slash.dismiss._approve_if_pr_fully_resolved`) — which
        only counts unresolved threads on reviews whose *current* state is
        `CHANGES_REQUESTED` — would no longer see them and could approve a
        PR with a High finding still open on the review this method just
        allowed to be dismissed.
        """
        return any(
            t.review_id == review_id and not t.is_resolved for t in all_threads
        )

    def _apply_thread_update(
        self, classified: Classified, *, enable_suggestions: bool
    ) -> bool:
        """PATCH a still-open thread's comment in place for an `update` or
        `escalate` classification. Returns whether the PATCH succeeded --
        the caller only counts `inline_updated` and posts an escalation
        reply when it did, since a failed PATCH leaves the comment showing
        the old content/severity and a reply claiming otherwise would itself
        be a silent-failure risk.

        The `suggestion` fence is only kept when the matched thread's
        anchored range exactly matches this finding's -- `PATCH` cannot move
        a comment's line/range, so a fuzzy match (up to `PROXIMITY_LINES` of
        drift) or an outdated thread would otherwise offer a one-click apply
        against the wrong lines, or fail to apply at all.
        """
        thread = classified.thread
        if thread is None:
            return False
        include_fence = (
            enable_suggestions
            and not thread.is_outdated
            and thread.line == classified.finding.line
            and thread.start_line == classified.finding.start_line
        )
        new_body = _build_inline_comment_body(
            classified.finding,
            finding_id=thread.finding_id,
            include_suggestion_fence=include_fence,
        )
        ok, status, snippet = self.update_review_comment(thread.comment_id, new_body)
        if not ok:
            _log.warning(
                "github: failed to update comment %d for a %s classification: "
                "HTTP %d", thread.comment_id, classified.kind, status,
            )
            self._errors.append(
                f"update comment {thread.comment_id}: HTTP {status}: {snippet}"
            )
        return ok

    def _notify_escalation(
        self, thread: PriorThread, finding: Finding, head_sha: str
    ) -> None:
        old_severity = thread.severity or "an unknown severity"
        message = (
            f"Severity escalated from **{old_severity}** to **{finding.severity}** "
            f"in the latest run (`{head_sha[:7]}`)."
        )
        ok, status, snippet = self.reply_to_review_comment(thread.comment_id, message)
        if not ok:
            _log.warning(
                "github: failed to post escalation reply on comment %d: HTTP %d",
                thread.comment_id, status,
            )
            self._errors.append(
                f"escalation reply {thread.comment_id}: HTTP {status}: {snippet}"
            )

    def _notify_recurrence(self, thread: PriorThread, head_sha: str) -> bool:
        """Reply on the recurred finding's thread and reopen it. Returns
        whether the thread was actually reopened (`unresolve_thread`
        succeeded) -- the caller uses this to keep its in-memory thread
        snapshot in sync, since a successful reopen changes the thread's
        resolved state on GitHub with no corresponding update to any
        already-fetched `PriorThread` (frozen, and fetched before this side
        effect runs)."""
        message = (
            "This finding was marked fixed but reappeared unchanged in the "
            f"latest run (`{head_sha[:7]}`)."
        )
        ok, status, snippet = self.reply_to_review_comment(thread.comment_id, message)
        if not ok:
            _log.warning(
                "github: failed to post recurrence reply on comment %d: HTTP %d",
                thread.comment_id, status,
            )
            self._errors.append(
                f"recurrence reply {thread.comment_id}: HTTP {status}: {snippet}"
            )
        ok2, status2, snippet2 = self.unresolve_thread(thread.thread_id)
        if not ok2:
            _log.warning(
                "github: failed to unresolve thread %s: HTTP %d",
                thread.thread_id, status2,
            )
            self._errors.append(
                f"unresolve thread {thread.thread_id}: HTTP {status2}: {snippet2}"
            )
        return ok2

    def _render_fallback_body(
        self, body: str, inline_comments: Sequence[dict[str, Any]]
    ) -> str:
        if not inline_comments:
            return body
        rendered = "\n".join(
            "- " + (c.get("body") or "").replace("\n", "\n  ") for c in inline_comments
        )
        section = "### Findings (inline anchoring unavailable)\n" + rendered
        if "All findings are attached as inline comments." in body:
            return body.replace(
                "All findings are attached as inline comments.", section
            )
        return f"{body}\n\n{section}"

    # ------------------------------------------------------------------
    # resolve_stale — marker-gated stale-thread resolution + review dismissal
    # ------------------------------------------------------------------
    def resolve_stale(self, current_review_id: int | None = None) -> StaleResult:
        # Single snapshot before any sub-calls write to self._errors; all new
        # entries from fetch_review_threads and _dismiss_stale_reviews are
        # collected via self._errors[errors_before:] at the end.
        errors_before = len(self._errors)
        threads = self.fetch_review_threads()
        resolved = 0
        skipped_no_marker = 0
        thread_errors: list[str] = []
        for thread in threads:
            if thread.get("isResolved"):
                continue
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id in self._kept_alive_thread_ids:
                # canonical-review reuse's post_findings just PATCHed this
                # thread's comment (update/escalate) or reopened it
                # (recurred) in this same run -- it corresponds to a still-
                # active finding, not something to resolve as stale (#718).
                continue
            body = _first_comment_body(thread)
            author = _first_comment_author_login(thread) or None
            # graphql_bot_login() strips the REST-style "[bot]" suffix
            # before comparing: GitHub's GraphQL API reports the bot's login
            # without it (verified live), so comparing the raw constant here
            # rejected every real thread (#717).
            if not is_owned_by_us(
                body, author, graphql_bot_login(self.config.bot_login), kind="inline"
            ):
                skipped_no_marker += 1
                continue
            if not isinstance(thread_id, str):
                continue
            ok, status, body_snippet = self.resolve_thread(thread_id)
            if not ok:
                thread_errors.append(
                    f"resolve thread {thread_id}: HTTP {status}: {body_snippet}"
                )
                continue
            resolved += 1

        dismissed = self._dismiss_stale_reviews(threads, current_review_id)

        return StaleResult(
            threads_resolved=resolved,
            reviews_dismissed=dismissed,
            threads_skipped_no_marker=skipped_no_marker,
            errors=tuple(thread_errors) + tuple(self._errors[errors_before:]),
        )

    def fetch_review_threads(self) -> list[dict[str, Any]]:
        query = (
            "query($owner:String!,$repo:String!,$pr:Int!,$after:String){"
            "repository(owner:$owner,name:$repo){pullRequest(number:$pr){"
            "reviewThreads(first:100,after:$after){"
            "pageInfo{hasNextPage endCursor}"
            "nodes{id isResolved path line originalLine startLine isOutdated "
            "comments(first:100){nodes{databaseId body author{login} "
            "pullRequestReview{databaseId}}}}}}}}"
        )
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            variables: dict[str, Any] = {
                "owner": self.config.owner,
                "repo": self.config.repo,
                "pr": self.config.pr_number,
                "after": cursor,
            }
            resp = self.client.request(
                "POST",
                _GRAPHQL_PATH,
                json_body={"query": query, "variables": variables},
            )
            if resp.status_code >= 400:
                self._errors.append(
                    f"fetch_review_threads: HTTP {resp.status_code}: {resp.text[:200]}"
                )
                break
            data = resp.json() or {}
            if data.get("errors"):
                msgs = "; ".join(
                    (e.get("message") or str(e)) for e in data["errors"]
                )
                self._errors.append(f"fetch_review_threads GraphQL error: {msgs}")
                break
            rt = (
                data.get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("reviewThreads", {})
            )
            threads.extend(rt.get("nodes") or [])
            page_info = rt.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        return threads

    def resolve_thread(self, thread_id: str) -> tuple[bool, int, str]:
        mutation = (
            "mutation($id:ID!){resolveReviewThread(input:{threadId:$id})"
            "{thread{id isResolved}}}"
        )
        resp = self.client.request(
            "POST",
            _GRAPHQL_PATH,
            json_body={"query": mutation, "variables": {"id": thread_id}},
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def dismiss_review(self, review_id: int, message: str) -> tuple[bool, int, str]:
        """PUT a dismissal for a single review. Returns (ok, status, body_snippet).

        Thin primitive with no policy about *which* review to dismiss or
        *when* — that decision belongs to the caller (e.g. `_dismiss_stale_reviews`
        for the review-posting path, or `ai_pr_review.slash.dismiss` for the
        slash-command path, which has different semantics: it dismisses the
        review whose thread was just resolved, not "all but the current run's
        review").
        """
        resp = self.client.request(
            "PUT",
            self._dismiss_url(review_id),
            json_body={"message": message},
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def update_review_body(self, review_id: int, body: str) -> tuple[bool, int, str]:
        """PUT a new body onto an existing review. Returns (ok, status, body_snippet).

        `PUT /pulls/{n}/reviews/{id}` can only ever change a review's body
        text, never its `state`/event (confirmed against GitHub's REST docs
        -- the only way to change state is the separate submit-events
        endpoint, which does not accept a body). Thin primitive, same shape
        as `dismiss_review`: no policy about *which* review to update or
        *when* -- that belongs to the caller (the canonical-review reuse
        path in `post_findings`).
        """
        resp = self.client.request(
            "PUT",
            self._review_url(review_id),
            json_body={"body": body},
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def get_review_state(self, review_id: int) -> str | None:
        """Fetch a single review's current `state` (e.g. `CHANGES_REQUESTED`,
        `DISMISSED`, `APPROVED`, `COMMENTED`).

        Returns `None` on any HTTP error (appended to `self._errors`). A
        dedicated single-review GET rather than a re-list via
        `list_bot_reviews()` — that method paginates the full review list and
        filters to bot-authored reviews, which is unnecessary work when the
        caller already has a specific `review_id` in hand (e.g.
        `ai_pr_review.slash.dismiss._dismiss_if_all_resolved`, which needs
        this immediately before a dismiss PUT to avoid attempting one against
        a review no longer in a dismissable state — issue #562).
        """
        resp = self.client.request("GET", self._review_url(review_id))
        if resp.status_code >= 400:
            self._errors.append(
                f"get_review_state {review_id}: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json() or {}
        state = data.get("state")
        return state if isinstance(state, str) else None

    def update_review_comment(self, comment_id: int, body: str) -> tuple[bool, int, str]:
        """PATCH an existing inline review comment's body in place. Returns
        (ok, status, body_snippet).

        Backs the canonical-review-reuse `update`/`escalate` paths in
        `post_findings`: a still-open, fuzzy-matched thread gets its comment
        body refreshed (severity, id-map/verdicts-independent per-comment
        metadata marker) without creating a new comment or review object.
        Thin primitive, same shape as `update_review_body`/`dismiss_review`:
        no policy about *which* comment or *when* -- that belongs to the
        caller.
        """
        resp = self.client.request(
            "PATCH", self._review_comment_url(comment_id), json_body={"body": body}
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def reply_to_review_comment(self, comment_id: int, body: str) -> tuple[bool, int, str]:
        """POST a reply into an existing review comment's thread. Returns
        (ok, status, body_snippet).

        Backs the `escalate` (severity-change notice) and `recurred`
        (fixed-then-reappeared notice) side effects in `post_findings` --
        both need to notify on the existing thread without creating a new
        top-level review object. No Python code posted a reply before this;
        the prior implementation of this interaction lived in
        `.github/workflows/slash-commands.yml` (bash), unrelated to this
        provider's own write paths.
        """
        resp = self.client.request(
            "POST", self._review_comment_reply_url(comment_id), json_body={"body": body}
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def unresolve_thread(self, thread_id: str) -> tuple[bool, int, str]:
        """GraphQL `unresolveReviewThread` -- the inverse of `resolve_thread`.

        Used when a `fixed`-flagged finding recurs unchanged (issue
        recurred): the thread was marked resolved by the `fixed` command,
        and reopening it makes the recurrence visible in the PR's
        Conversation tab as an active thread again, alongside the reply
        `post_findings` posts explaining why.
        """
        mutation = (
            "mutation($id:ID!){unresolveReviewThread(input:{threadId:$id})"
            "{thread{id isResolved}}}"
        )
        resp = self.client.request(
            "POST",
            _GRAPHQL_PATH,
            json_body={"query": mutation, "variables": {"id": thread_id}},
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def get_review_state_and_body(self, review_id: int) -> tuple[str, str] | None:
        """Fetch a single review's current `(state, body)`. Returns `None` on
        any HTTP error (appended to `self._errors`) or a response missing a
        usable `state`.

        A sibling to `get_review_state` (which returns only `state`, for the
        pre-existing `slash/dismiss.py` call sites this method deliberately
        does not touch) rather than a breaking change to that method's
        contract. Used by `post_findings`'s pre-write concurrency re-check
        immediately before PUTing/PATCHing the canonical review: if the
        state or body has changed since the canonical was selected --
        another run or a slash command wrote to it in the meantime -- the
        write is abandoned in favor of posting a fresh review rather than
        clobbering the concurrent write.
        """
        resp = self.client.request("GET", self._review_url(review_id))
        if resp.status_code >= 400:
            self._errors.append(
                f"get_review_state_and_body {review_id}: HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return None
        data = resp.json()
        # Defensive: a malformed/unexpected response shape (e.g. a list, if a
        # caller or test double ever routes this to the list-reviews
        # endpoint by mistake) must fail soft, not raise -- this feeds a
        # concurrency re-check whose only contract is "None means don't
        # trust this write."
        if not isinstance(data, dict):
            self._errors.append(
                f"get_review_state_and_body {review_id}: unexpected response shape "
                f"{type(data).__name__}"
            )
            return None
        state = data.get("state")
        if not isinstance(state, str):
            return None
        body = data.get("body")
        return state, (body if isinstance(body, str) else "")

    def get_pr_head_sha(self) -> str | None:
        """GET the PR's current head SHA. Returns `None` on any HTTP error
        (appended to `self._errors`) or a response missing a usable SHA.

        Used by `post_findings`'s concurrency guard: if the PR's head no
        longer equals the SHA this run computed its diff against, a newer
        run is already in flight (or about to be) -- the canonical write is
        skipped entirely (treated as a successful no-op) rather than racing
        to overwrite what that newer run is about to post.
        """
        resp = self.client.request("GET", self._pull_request_url())
        if resp.status_code >= 400:
            self._errors.append(
                f"get_pr_head_sha: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json()
        if not isinstance(data, dict):
            self._errors.append(
                f"get_pr_head_sha: unexpected response shape {type(data).__name__}"
            )
            return None
        head = data.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        return sha if isinstance(sha, str) else None

    def submit_approval(self, message: str) -> tuple[bool, int, str]:
        """POST a standalone APPROVE review. Returns (ok, status, body_snippet).

        A thin, lightweight primitive for the slash-command "approve on
        clear" path (issue #590): GitHub's REST API has no endpoint to
        convert an existing review's state, so the only way to reach an
        `APPROVED` `reviewDecision` after a `dismiss`/`false-positive`/
        `wont-fix` command clears the last active finding is to dismiss the
        stale `CHANGES_REQUESTED` review(s) (`dismiss_review`) and then POST a
        brand-new review with `event: "APPROVE"`.

        Deliberately does not reuse `post_findings`: that method's APPROVE
        path exists to post a whole findings summary (with an optional
        pre-APPROVE COMMENT review to carry inline comments GitHub disallows
        on an APPROVE payload) after a fresh analysis run. This primitive has
        no findings to render and no inline comments to attach — it only
        needs `commit_id` omitted (defaults to the PR's current head on
        GitHub's side) and a short attribution body, mirroring
        `dismiss_review`'s minimal-payload style.
        """
        resp = self.client.request(
            "POST",
            self._reviews_url(),
            json_body={"body": message, "event": "APPROVE"},
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]

    def post_check_run(
        self, head_sha: str, name: str, conclusion: str, title: str, summary: str
    ) -> bool:
        """Create a GitHub check run reporting whether a required policy.yml
        tier (see docs/policy.md) has run for `head_sha`.

        GitHub-only (no GitLab/Bitbucket equivalent is wired here) — a
        follow-up, not required for the policy.yml routing feature itself.
        This is intentionally a create-only, fire-and-forget primitive: each
        invocation (the automatic review, or a later `/ai-pr-review
        review-full`) posts its own check run for the current head_sha, so
        a later run naturally supersedes an earlier `action_required` one in
        the Checks tab without needing to look up or update a prior run's ID.
        Never raises — a failure to post is logged to self._errors and
        must never block or fail the review itself (fail-soft, matching
        every other best-effort posting step in this provider).

        `conclusion` must be a valid GitHub check-run conclusion value —
        this method only ever calls it with "success" or "action_required"
        (see cli.py's post-review policy-gate step). "failure" is
        deliberately never used here (see docs/policy.md's rationale: an
        unmet requirement on the automatic push is not itself a failure,
        only an unactioned manual step) — "action_required" carries that
        softer meaning while still excluding the check from GitHub's
        required-status-check pass set (`success`/`neutral`/`skipped`
        only), so it actually blocks merge like a real gate. A plain
        "neutral" conclusion was tried first and found to satisfy required
        status checks the same as "success", silently defeating the gate
        (see #688) — do not revert to "neutral" here.
        """
        resp = self.client.request(
            "POST",
            self._check_runs_url(),
            json_body={
                "name": name,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title, "summary": summary},
            },
        )
        if resp.status_code >= 400:
            self._errors.append(
                f"post_check_run: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return False
        return True

    def fetch_review_comment(self, comment_id: int) -> dict[str, Any] | None:
        """Fetch a single PR review (inline) comment by its REST databaseId.

        Returns `{"login": ..., "path": ..., "body": ...}` or `None` on any
        HTTP error (appended to `self._errors`) or non-2xx response. Used by
        `feedback-command`'s context-extraction step, which needs the parent
        comment's author, file path, and rendered body to derive
        source/file/rule_id for the FeedbackEntry.
        """
        resp = self.client.request("GET", self._review_comment_url(comment_id))
        if resp.status_code >= 400:
            self._errors.append(
                f"fetch_review_comment {comment_id}: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json() or {}
        return {
            "login": (data.get("user") or {}).get("login") or "",
            "path": data.get("path") or "",
            "body": data.get("body") or "",
        }

    def list_bot_reviews(self) -> list[dict[str, Any]]:
        """Return all reviews authored by our bot login, paginated.

        Factored out of `_dismiss_stale_reviews`, which used to inline this
        same paginated `/pulls/{n}/reviews` walk. `_list_prior_bot_review_bodies`
        is intentionally left as its own walk (not rebased on this method): it
        returns `[]` on a mid-pagination HTTP error rather than partial results,
        a #550/#553 guarantee this method does not preserve (it appends to
        `self._errors` and returns whatever it collected so far instead).
        """
        c = self.config
        reviews: list[dict[str, Any]] = []
        url: str | None = self._reviews_url()
        params: dict[str, Any] | None = {"per_page": 100}
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                self._errors.append(
                    f"list reviews: HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return reviews
            for review in resp.json() or []:
                if (review.get("user") or {}).get("login") == c.bot_login:
                    reviews.append(review)
            url = _parse_next_link(resp.headers.get("link", ""))
            params = None
        return reviews

    def _dismiss_stale_reviews(
        self,
        threads: Sequence[dict[str, Any]],
        current_review_id: int | None,
    ) -> int:
        """Dismiss CHANGES_REQUESTED reviews from our bot whose threads are all
        resolved, but only when at least one such thread was authored by us
        (marker gate via thread-body check already applied above).

        current_review_id: the review ID the current run posted (may be an APPROVE
        or COMMENT review, not necessarily CHANGES_REQUESTED). Only this exact review
        is protected from dismissal. When None, we have no knowledge of what the
        current run posted, so we leave all CR reviews intact as a safety guard.

        This correctly handles the case where the current run posts APPROVE (0
        findings): the sole remaining CHANGES_REQUESTED review is the stale one from
        the prior run and must be dismissed.
        """
        if current_review_id is None:
            # Cannot determine which CR review the current run posted (degraded path).
            # Leave all CR reviews intact rather than risk dismissing an active one.
            return 0

        reviews = self.list_bot_reviews()

        # Map review id -> unresolved thread count, only counting threads
        # where the comment body carries OUR inline marker.
        unresolved_by_review: dict[int, int] = {}
        for t in threads:
            if t.get("isResolved"):
                continue
            body = _first_comment_body(t)
            author = _first_comment_author_login(t) or None
            # See resolve_stale's matching comment (#717).
            if not is_owned_by_us(
                body, author, graphql_bot_login(self.config.bot_login), kind="inline"
            ):
                continue
            rid = _first_comment_review_id(t)
            if rid is None:
                continue
            unresolved_by_review[rid] = unresolved_by_review.get(rid, 0) + 1

        dismissed = 0
        for review in reviews:
            if review.get("state") != "CHANGES_REQUESTED":
                continue
            rid = _safe_int(review.get("id"))
            if rid <= 0:
                continue
            if rid == current_review_id:
                continue  # never dismiss the review the current run posted
            if unresolved_by_review.get(rid, 0) > 0:
                continue
            _log.debug("github: dismissing stale review %d", rid)
            ok, status, body_snippet = self.dismiss_review(
                rid, "Superseded by a subsequent review run."
            )
            if ok:
                dismissed += 1
            else:
                _log.warning(
                    "github: failed to dismiss review %d: HTTP %d", rid, status
                )
                self._errors.append(
                    f"dismiss review {rid}: HTTP {status}: {body_snippet}"
                )
        return dismissed


def _render_review_body(
    *,
    event: PostEvent,
    findings: Sequence[Finding],
    inline_count: int,
    body_findings_text: str,
    out_of_diff_findings_text: str = "",
    failed_agents: Sequence[str],
    token_table: str,
    agent_prompt: str,
) -> str:
    """Compose the review body for GitHub's reviews API."""
    from ai_pr_review.vcs._body import severity_icon

    # The headline risk/count come from the shared compute_headline() helper
    # (vcs/_body.py) rather than being recomputed independently here — this is
    # the #622 fix. Only genuine out_of_diff findings (analyzer findings
    # outside the changed-line set, always capped to Low) are excluded; a
    # judge-downranked finding (demoted_to_body=True) counts at its true
    # severity, matching review.outcome.classify_review_outcome's decision.
    # NOTE: the review event (APPROVE / REQUEST_CHANGES) is determined by the
    # caller before this function is invoked.
    headline = compute_headline(findings, failed_agents)
    finding_total = headline.count
    risk = headline.risk
    ood_count = len(findings) - finding_total
    footer = (
        "\n\n---\n*AI Review — generated by "
        "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
    )

    def _ood_section() -> str:
        if not out_of_diff_findings_text:
            return ""
        return (
            f"\n\n<details>\n<summary>🔵 Out-of-diff analyzer findings ({ood_count})"
            " — pre-existing issues on unchanged lines, capped to Low</summary>\n\n"
            f"{out_of_diff_findings_text}\n</details>"
        )

    if event == "APPROVE":
        if finding_total == 0:
            body = (
                "## AI Review: Approved\n\n"
                "No findings above the confidence threshold. The changes look good."
            )
        else:
            body = (
                "## AI Review: Approved\n\n"
                f"{severity_icon(risk)} **Overall Risk:** {risk} | "
                f"**Findings:** {finding_total} ({inline_count} inline)\n\n"
                "No Critical or High findings. The changes look good — "
                "Medium/Low findings are informational only."
            )
            if body_findings_text:
                body += f"\n\n### Findings (informational)\n{body_findings_text}"
        body += _ood_section()
        if token_table:
            body += f"\n\n{token_table}"
        return body + footer

    if event == "COMMENT" and failed_agents and finding_total == 0:
        joined = ", ".join(failed_agents)
        body = (
            "## AI Review: Incomplete\n\n"
            "No findings above the confidence threshold, but one or more agents "
            f"failed: {joined}\n\n"
            "The review may be incomplete. Please verify manually or re-run the review."
        )
        body += _ood_section()
        if token_table:
            body += f"\n\n{token_table}"
        return body + footer

    # COMMENT with findings or REQUEST_CHANGES
    body = (
        "## AI Review Findings\n\n"
        f"{severity_icon(risk)} **Overall Risk:** {risk} | "
        f"**Findings:** {finding_total} ({inline_count} inline)"
    )
    if body_findings_text:
        body += f"\n\n### Findings not attached to specific lines\n{body_findings_text}"
    elif inline_count > 0:
        body += "\n\nAll findings are attached as inline comments."
    body += _ood_section()
    if token_table:
        body += f"\n\n{token_table}"
    body += footer
    if agent_prompt:
        body += f"\n\n{agent_prompt}"
    return body


def _build_inline_comment_body(
    f: Finding, *, finding_id: int | None = None, include_suggestion_fence: bool = True
) -> str:
    """Render the markdown body for a GitHub inline review comment.

    Parameters
    ----------
    finding_id:
        Optional stable per-PR ID (e.g. 3 → ``**[F3]**``).  When provided,
        the token is inserted between severity and source tag, mirroring
        the body-finding render so all findings have a consistent ID token
        regardless of whether they are anchored inline or fall to the body.
    include_suggestion_fence:
        Set `False` when re-rendering an *existing* comment in place
        (canonical-review-reuse `update`/`escalate` paths) and the matched
        thread's anchored line/range no longer exactly matches this
        finding's (a fuzzy match tolerates up to `PROXIMITY_LINES` of drift,
        or the thread is outdated) -- `PATCH` cannot move a comment's
        anchored line/range, so a mismatched ```suggestion``` fence would
        either silently fail to offer the one-click apply or offer it
        against the wrong lines. The proposed code still renders, just as a
        plain fence without the one-click affordance.
    """
    from ai_pr_review.vcs._body import format_source_tag, sanitize_display_text, severity_icon
    from ai_pr_review.vcs._finding_ids import fingerprint
    from ai_pr_review.vcs.marker import build_inline_meta_marker

    icon = severity_icon(f.severity)
    tag = format_source_tag(f)
    id_token = f" **[F{finding_id}]**" if finding_id is not None else ""
    header = f"{icon} **[{f.severity}]**{id_token} {tag} {sanitize_display_text(f.finding)}".strip()
    parts = [header]
    if f.remediation:
        parts.append(f"\n**Remediation:** {sanitize_display_text(f.remediation)}")
    if f.suggested_code and "```" not in f.suggested_code:
        fence = "suggestion" if include_suggestion_fence else ""
        parts.append(f"\n```{fence}\n{f.suggested_code}\n```")
    body = "".join(parts)
    # Attach inline marker so resolve_stale can identify ownership later,
    # then the per-comment metadata marker (canonical-review reuse, read
    # side) carrying this finding's exact fingerprint/category/severity --
    # see vcs/marker.py's module comment for why it's appended last and
    # base64-encoded.
    body = append_inline_marker(body)
    meta_marker = build_inline_meta_marker(
        fingerprint=fingerprint(f), category=f.category, severity=f.severity
    )
    return f"{body}\n{meta_marker}"


def _carried_forward_finding(thread: PriorThread) -> Finding:
    """Minimal `Finding` stand-in for an open owned thread this run's
    classification pass never matched (neither suppressed, recurred,
    updated, nor escalated) -- fed only to `compute_headline` (via
    `_render_review_body`) so the rendered headline ("Findings: N") counts
    the PR's currently-active findings, not just this run's diff. Never
    rendered as an actual finding of its own; `thread.severity`/`category`
    default to `"Low"`/`"other"` when unrecoverable (legacy thread, no
    metadata marker) -- an undercount is the safer failure than inventing a
    severity that was never actually observed.
    """
    return Finding(
        severity=cast("Any", thread.severity or "Low"),
        confidence=0,
        finding="(carried forward from a still-open thread)",
        category=cast("Any", thread.category or "other"),
        file=thread.path,
        line=thread.line,
    )


def _build_inline_comment_payload(
    f: Finding,
    *,
    eligible_new: set[tuple[str, int]],
    eligible_context: set[tuple[str, int]],
    enable_suggestions: bool,
    finding_id: int | None = None,
) -> dict[str, Any] | None:
    """Return a GitHub reviews-API inline-comment dict, or None if ineligible.

    Eligibility logic delegated to ai_pr_review.vcs._inline so all providers
    share identical diff-anchor / suggestion-range / fence-escape rules.
    """
    if not is_inline_eligible(f, eligible_new):
        return None

    body = _build_inline_comment_body(f, finding_id=finding_id)
    payload: dict[str, Any] = {"path": f.file, "line": f.line, "body": body}

    if (
        enable_suggestions
        and is_suggestion_safe(f)
        and is_suggestion_range_valid(f, eligible_context=eligible_context)
        and f.start_line is not None
    ):
        payload["start_line"] = f.start_line

    return payload


def _first_comment_body(thread: dict[str, Any]) -> str:
    nodes = ((thread.get("comments") or {}).get("nodes")) or []
    if not nodes:
        return ""
    return (nodes[0].get("body") or "")


def _first_comment_author_login(thread: dict[str, Any]) -> str:
    nodes = ((thread.get("comments") or {}).get("nodes")) or []
    if not nodes:
        return ""
    author = nodes[0].get("author") or {}
    return author.get("login") or ""


def _first_comment_review_id(thread: dict[str, Any]) -> int | None:
    nodes = ((thread.get("comments") or {}).get("nodes")) or []
    if not nodes:
        return None
    pr_review = nodes[0].get("pullRequestReview") or {}
    rid = pr_review.get("databaseId")
    if isinstance(rid, int):
        return rid
    return None
