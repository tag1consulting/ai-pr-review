"""GitHub VCS provider — ports post-review.sh.

Implements the VcsProvider protocol for GitHub REST + GraphQL. All stale
cleanup is marker-gated (closes #183, #184); cleanup runs only after a
successful post (2.FR-10).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import truncate_body
from ai_pr_review.vcs._inline import (
    is_inline_eligible,
    is_suggestion_range_valid,
    is_suggestion_safe,
)
from ai_pr_review.vcs._stale import is_owned_by_us
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
    SUMMARY_MARKER_PREFIX,
    append_inline_marker,
    build_summary_marker,
    extract_summary_sha,
    replace_summary_sha,
)
from ai_pr_review.vcs.protocol import (
    DiffContext,
    FindingsResult,
    PostEvent,
    StaleResult,
    SummaryResult,
)

_log = logging.getLogger(__name__)

_BOT_LOGIN_DEFAULT: Final[str] = "github-actions[bot]"

_GRAPHQL_PATH: Final[str] = "/graphql"


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
    # Prior bot review bodies — used for body-finding ID reconstruction
    # ------------------------------------------------------------------
    def _list_prior_bot_review_bodies(self) -> list[str]:
        """Return the body text of all prior bot CHANGES_REQUESTED reviews.

        Used by the body-finding ID-map assembler to reconstruct which
        ``[F<n>]`` IDs have already been assigned on this PR.  On error,
        returns an empty list so rendering degrades gracefully (IDs restart
        at 1 rather than failing).
        """
        c = self.config
        bodies: list[str] = []
        url: str | None = self._reviews_url()
        params: dict[str, Any] | None = {"per_page": 100}
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                _log.warning(
                    "github: could not list reviews for ID-map assembly: HTTP %d",
                    resp.status_code,
                )
                return []
            for review in resp.json() or []:
                if (review.get("user") or {}).get("login") != c.bot_login:
                    continue
                if review.get("state") not in (
                    "CHANGES_REQUESTED", "COMMENTED", "APPROVED", "DISMISSED"
                ):
                    continue
                body = review.get("body") or ""
                if "### Findings not attached to specific lines" in body:
                    bodies.append(body)
            url = _parse_next_link(resp.headers.get("link", ""))
            params = None
        return bodies

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
    # post_skip_comment — inline-marker-bearing no-op comment
    # ------------------------------------------------------------------
    def post_skip_comment(self, reason: str) -> SummaryResult:
        body = append_inline_marker(
            f"**AI Review skipped.** {reason.strip() or 'No changes to review.'}"
        )
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
        keep = existing[-1]
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
        from ai_pr_review.diff.linemap import parse_added_lines, parse_new_file_lines
        from ai_pr_review.vcs._body import format_body_finding, join_findings
        from ai_pr_review.vcs._finding_ids import assemble_id_map, fingerprint

        eligible_new = {(lr.file, lr.line) for lr in parse_added_lines(diff.diff_text)}
        eligible_ctx = {
            (lr.file, lr.line) for lr in parse_new_file_lines(diff.diff_text)
        }

        # Assign stable per-PR IDs to ALL findings upfront — inline and body
        # alike — so the ID counter is consistent regardless of where a finding
        # ends up rendered. Users can then reference any finding by its F<n> ID
        # from a top-level comment, not just body-level ones.
        prior_bodies: list[str] = []
        if findings:
            try:
                prior_bodies = self._list_prior_bot_review_bodies()
            except Exception as exc:  # noqa: BLE001
                import os as _os
                # Emit a GitHub Actions ::warning:: annotation only when running
                # inside GitHub Actions to avoid polluting local/test output.
                if _os.environ.get("GITHUB_ACTIONS") == "true":
                    print(
                        f"::warning::ai-pr-review: failed to fetch prior review bodies; "
                        f"body-finding IDs may not be stable this cycle: {exc}",
                        flush=True,
                    )
                _log.warning(
                    "github: failed to fetch prior review bodies for ID map; "
                    "body-finding IDs may not be stable: %s", exc,
                )
        id_map = assemble_id_map(prior_bodies, list(findings))

        inline_comments: list[dict[str, Any]] = []
        original_inline_comments: list[dict[str, Any]] = []
        body_findings: list[Finding] = []
        for f in findings:
            if len(inline_comments) < max_inline:
                payload = _build_inline_comment_payload(
                    f,
                    eligible_new=eligible_new,
                    eligible_context=eligible_ctx,
                    enable_suggestions=enable_suggestions,
                    finding_id=id_map.get(fingerprint(f)),
                )
                if payload is not None:
                    inline_comments.append(payload)
                    continue
            # Fell through to body-findings
            body_findings.append(f)

        body_bullets: list[str] = []
        for f in body_findings:
            loc_note = ""
            if f.file and f.line is not None and (f.file, f.line) not in eligible_new:
                loc_note = " *(line not in diff)*"
            body_bullets.append(
                format_body_finding(
                    f,
                    location_note=loc_note,
                    finding_id=id_map.get(fingerprint(f)),
                )
            )

        # Build body
        body = _render_review_body(
            event=event,
            findings=findings,
            inline_count=len(inline_comments),
            body_findings_text=join_findings(body_bullets),
            failed_agents=failed_agents,
            token_table=token_table,
            agent_prompt=agent_prompt,
        )
        # Embed the ID map as a hidden HTML comment so future reviews and the
        # dismiss workflow can reconstruct which fingerprint → ID associations
        # exist on this PR without needing to parse rendered bullet text.
        from ai_pr_review.vcs.marker import build_id_map_marker
        id_map_marker = ""
        try:
            id_map_marker = build_id_map_marker(id_map)
        except Exception as exc:  # noqa: BLE001
            _log.warning("github: failed to build id-map marker: %s", exc)
        # Body content takes priority over the id-map marker.  Check whether
        # the marker fits before computing truncate_limit so that a large marker
        # doesn't shrink the visible review body; drop the marker first instead.
        _MIN_BODY_BYTES = 4096
        from ai_pr_review.vcs._body import GITHUB_MAX_BODY_SIZE
        marker_bytes = len(id_map_marker.encode("utf-8")) if id_map_marker else 0
        # +1 for the newline separator between body and marker
        marker_reserve = marker_bytes + 1 if id_map_marker else 0
        if id_map_marker and marker_reserve > GITHUB_MAX_BODY_SIZE - _MIN_BODY_BYTES:
            _log.warning(
                "github: id-map marker (%d bytes) too large to fit in review body "
                "for %s/%s PR #%s; omitting marker for this cycle — ID stability may degrade",
                marker_bytes,
                self.config.owner, self.config.repo, self.config.pr_number,
            )
            id_map_marker = ""
            marker_reserve = 0
        # Floor at 0 as a defensive guard; marker_reserve is always ≤ GITHUB_MAX_BODY_SIZE
        # after the drop-if-too-large check above, but clamp to prevent any edge case
        # producing a negative limit.
        truncate_limit = max(0, GITHUB_MAX_BODY_SIZE - marker_reserve)
        body = append_inline_marker(truncate_body(body, limit=truncate_limit))
        if id_map_marker:
            body += "\n" + id_map_marker

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
                body_findings=len(body_bullets),
                event=event,
                degraded_to_comment=False,
            )

        # Fallback 1: retry as COMMENT (GITHUB_TOKEN may not be able to block/approve)
        self._errors.append(
            f"post review ({event}): HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if event in ("APPROVE", "REQUEST_CHANGES"):
            review_payload["event"] = "COMMENT"
            resp2 = self.client.request(
                "POST", self._reviews_url(), json_body=review_payload
            )
            if resp2.status_code < 400:
                data = resp2.json() or {}
                return FindingsResult(
                    review_id=int(data.get("id", 0)) or None,
                    inline_posted=inline_posted_count + len(inline_comments),
                    body_findings=len(body_bullets),
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
                body_findings=len(body_bullets) + len(fallback_inline),
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
    def resolve_stale(self) -> StaleResult:
        # Single snapshot before any sub-calls write to self._errors; all new
        # entries from _fetch_review_threads and _dismiss_stale_reviews are
        # collected via self._errors[errors_before:] at the end.
        errors_before = len(self._errors)
        threads = self._fetch_review_threads()
        resolved = 0
        skipped_no_marker = 0
        thread_errors: list[str] = []
        for thread in threads:
            if thread.get("isResolved"):
                continue
            body = _first_comment_body(thread)
            author = _first_comment_author_login(thread) or None
            if not is_owned_by_us(body, author, self.config.bot_login, kind="inline"):
                skipped_no_marker += 1
                continue
            thread_id = thread.get("id")
            if not isinstance(thread_id, str):
                continue
            ok, status, body_snippet = self._resolve_thread(thread_id)
            if not ok:
                thread_errors.append(
                    f"resolve thread {thread_id}: HTTP {status}: {body_snippet}"
                )
                continue
            resolved += 1

        dismissed = self._dismiss_stale_reviews(threads)

        return StaleResult(
            threads_resolved=resolved,
            reviews_dismissed=dismissed,
            threads_skipped_no_marker=skipped_no_marker,
            errors=tuple(thread_errors) + tuple(self._errors[errors_before:]),
        )

    def _fetch_review_threads(self) -> list[dict[str, Any]]:
        query = (
            "query($owner:String!,$repo:String!,$pr:Int!,$after:String){"
            "repository(owner:$owner,name:$repo){pullRequest(number:$pr){"
            "reviewThreads(first:100,after:$after){"
            "pageInfo{hasNextPage endCursor}"
            "nodes{id isResolved "
            "comments(first:1){nodes{body author{login} "
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

    def _resolve_thread(self, thread_id: str) -> tuple[bool, int, str]:
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

    def _dismiss_stale_reviews(self, threads: Sequence[dict[str, Any]]) -> int:
        """Dismiss CHANGES_REQUESTED reviews from our bot whose threads are all
        resolved, but only when at least one such thread was authored by us
        (marker gate via thread-body check already applied above).

        The newest bot CHANGES_REQUESTED review (highest ID) is never dismissed
        — it represents the current run and must remain until superseded by a
        future run.  Only older reviews with zero unresolved markered threads
        are eligible.
        """
        c = self.config
        reviews: list[dict[str, Any]] = []
        url: str | None = f"/repos/{c.owner}/{c.repo}/pulls/{c.pr_number}/reviews"
        params: dict[str, Any] | None = {"per_page": 100}
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                self._errors.append(
                    f"list reviews: HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return 0
            reviews.extend(resp.json() or [])
            url = _parse_next_link(resp.headers.get("link", ""))
            params = None

        # Identify all bot CHANGES_REQUESTED reviews so we can protect the newest.
        bot_cr_ids: list[int] = [
            rid
            for r in reviews
            if r.get("state") == "CHANGES_REQUESTED"
            and (r.get("user") or {}).get("login") == c.bot_login
            for rid in (_safe_int(r.get("id")),)
            if rid > 0
        ]
        if len(bot_cr_ids) < 2:
            # Zero reviews: nothing to dismiss.
            # Exactly one review: it is the current run — never dismiss it.
            return 0
        newest_id = max(bot_cr_ids)

        # Map review id -> unresolved thread count, only counting threads
        # where the comment body carries OUR inline marker.
        unresolved_by_review: dict[int, int] = {}
        for t in threads:
            if t.get("isResolved"):
                continue
            body = _first_comment_body(t)
            author = _first_comment_author_login(t) or None
            if not is_owned_by_us(body, author, self.config.bot_login, kind="inline"):
                continue
            rid = _first_comment_review_id(t)
            if rid is None:
                continue
            unresolved_by_review[rid] = unresolved_by_review.get(rid, 0) + 1

        dismissed = 0
        for review in reviews:
            if review.get("state") != "CHANGES_REQUESTED":
                continue
            if (review.get("user") or {}).get("login") != c.bot_login:
                continue
            rid = _safe_int(review.get("id"))
            if rid <= 0:
                continue
            if rid == newest_id:
                continue  # never dismiss the current run
            if unresolved_by_review.get(rid, 0) > 0:
                continue
            _log.debug("github: dismissing stale review %d", rid)
            resp = self.client.request(
                "PUT",
                self._dismiss_url(rid),
                json_body={"message": "Superseded by a subsequent review run."},
            )
            if resp.status_code < 400:
                dismissed += 1
            else:
                _log.warning(
                    "github: failed to dismiss review %d: HTTP %d",
                    rid, resp.status_code,
                )
                self._errors.append(
                    f"dismiss review {rid}: HTTP {resp.status_code}: {resp.text[:200]}"
                )
        return dismissed


def _render_review_body(
    *,
    event: PostEvent,
    findings: Sequence[Finding],
    inline_count: int,
    body_findings_text: str,
    failed_agents: Sequence[str],
    token_table: str,
    agent_prompt: str,
) -> str:
    """Compose the review body for GitHub's reviews API."""
    from ai_pr_review.vcs._body import severity_icon

    finding_total = len(findings)
    # Top risk (only for display)
    def _top_risk() -> str:
        if finding_total == 0:
            return "None" if not failed_agents else "Unknown"
        for level in ("Critical", "High", "Medium", "Low"):
            if any(f.severity == level for f in findings):
                return level
        return "Low"

    risk = _top_risk()
    footer = (
        "\n\n---\n*AI Review — generated by "
        "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
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
    if token_table:
        body += f"\n\n{token_table}"
    body += footer
    if agent_prompt:
        body += f"\n\n{agent_prompt}"
    return body


def _build_inline_comment_body(f: Finding, *, finding_id: int | None = None) -> str:
    """Render the markdown body for a GitHub inline review comment.

    Parameters
    ----------
    finding_id:
        Optional stable per-PR ID (e.g. 3 → ``**[F3]**``).  When provided,
        the token is inserted between severity and source tag, mirroring
        the body-finding render so all findings have a consistent ID token
        regardless of whether they are anchored inline or fall to the body.
    """
    from ai_pr_review.vcs._body import format_source_tag, severity_icon

    icon = severity_icon(f.severity)
    tag = format_source_tag(f)
    id_token = f" **[F{finding_id}]**" if finding_id is not None else ""
    header = f"{icon} **[{f.severity}]**{id_token} {tag} {f.finding}".strip()
    parts = [header]
    if f.remediation:
        parts.append(f"\n**Remediation:** {f.remediation}")
    if f.suggested_code and "```" not in f.suggested_code:
        parts.append(f"\n```suggestion\n{f.suggested_code}\n```")
    body = "".join(parts)
    # Attach inline marker so resolve_stale can identify ownership later
    return append_inline_marker(body)


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
