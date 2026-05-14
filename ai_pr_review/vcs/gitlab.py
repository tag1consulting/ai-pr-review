"""GitLab VCS provider — ports post-review-gitlab.sh.

Implements the VcsProvider protocol for GitLab REST v4. Marker-gated stale
cleanup (closes the GitLab half of #184); cleanup runs after a successful
post (2.FR-10).

Provider differences from GitHub:
- Single combined note (summary + findings) rather than separate review.
- Discussions API for inline; position JSON requires base_sha/start_sha/head_sha.
- Token type detection picks the auth header: glpat-* (PRIVATE-TOKEN),
  glcbt-* (JOB-TOKEN), else OAuth2 Bearer.
- Suggestion fence syntax: ```suggestion:-N+0 for multi-line.
- Pagination via ?per_page=100&page=N (no Link header).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import (
    format_source_tag,
    severity_icon,
    truncate_body,
)
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

# GitLab MR notes have a ~1MB limit but huge comments are bad UX. Match bash.
_MAX_GITLAB_BODY_SIZE: Final[int] = 250_000

# Sentinel returned by _get_bot_username when a 4xx response indicates the
# token is invalid. Callers that distinguish hard-auth-failure from "unknown"
# check for this value and abort rather than degrading.
_BOT_IDENTITY_AUTH_FAILED: Final[str] = "__auth_failed__"


@dataclass(frozen=True)
class GitLabConfig:
    """Identifies the GitLab MR target and auth credentials."""

    project_id_or_path: str  # numeric ID or "group/project" path
    mr_iid: int  # MR internal ID (the human-readable !123 number)
    token: str
    diff_base_sha: str  # required for inline discussion positions
    bot_username: str | None = None  # if None, fetch from /user
    base_url: str = "https://gitlab.com/api/v4"


def _auth_header(token: str) -> tuple[str, str]:
    """Return (header_name, header_value) per GitLab token type."""
    if token.startswith("glpat-"):
        return ("PRIVATE-TOKEN", token)
    if token.startswith("glcbt-"):
        return ("JOB-TOKEN", token)
    return ("Authorization", f"Bearer {token}")


def _project_path_segment(project_id_or_path: str) -> str:
    """URL-encode the project segment for path-based project IDs."""
    if project_id_or_path.isdigit():
        return project_id_or_path
    # Use quote with safe="" so "/" gets encoded to %2F.
    return quote(project_id_or_path, safe="")


def build_client(
    config: GitLabConfig, retry: RetryPolicy | None = None
) -> RecordingClient:
    """Build a RecordingClient preconfigured for GitLab API calls."""
    header_name, header_value = _auth_header(config.token)
    http = httpx.Client(
        base_url=config.base_url,
        headers={
            header_name: header_value,
            "Accept": "application/json",
        },
        timeout=30.0,
    )
    return RecordingClient(
        http=http,
        recorder=TapeRecorder.from_env(provider="gitlab"),
        retry_policy=retry or RetryPolicy(),
    )


@dataclass
class GitLabProvider:
    """GitLab REST v4 implementation of VcsProvider."""

    config: GitLabConfig
    client: RecordingClient
    _errors: list[str] = field(default_factory=list, init=False, repr=False)
    _resolved_bot_username: str | None = field(
        default=None, init=False, repr=False
    )

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------
    def _project_segment(self) -> str:
        return _project_path_segment(self.config.project_id_or_path)

    def _notes_url(self) -> str:
        return f"/projects/{self._project_segment()}/merge_requests/{self.config.mr_iid}/notes"

    def _note_url(self, note_id: int) -> str:
        return f"{self._notes_url()}/{note_id}"

    def _discussions_url(self) -> str:
        return (
            f"/projects/{self._project_segment()}/merge_requests/"
            f"{self.config.mr_iid}/discussions"
        )

    def _discussion_resolve_url(self, discussion_id: str) -> str:
        return f"{self._discussions_url()}/{discussion_id}"

    # ------------------------------------------------------------------
    # Pagination helpers
    # ------------------------------------------------------------------
    def _list_summary_notes(self) -> list[dict[str, Any]]:
        """Return all MR notes whose body contains SUMMARY_MARKER_PREFIX.

        Iterates pages until a partial page is returned. Order: most recent
        first (sort=desc, order_by=updated_at) — same as bash.
        """
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self.client.request(
                "GET",
                self._notes_url(),
                params={
                    "per_page": 100,
                    "page": page,
                    "sort": "desc",
                    "order_by": "updated_at",
                },
            )
            if resp.status_code >= 400:
                self._errors.append(
                    f"list_summary_notes p{page}: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return results
            page_data = resp.json() or []
            for item in page_data:
                body = item.get("body") or ""
                if SUMMARY_MARKER_PREFIX in body:
                    results.append(item)
            if len(page_data) < 100:
                break
            page += 1
        return results

    # ------------------------------------------------------------------
    # Bot identity (lazy-resolved)
    # ------------------------------------------------------------------
    def _get_bot_username(self) -> str | None:
        """Fetch the bot's username via GET /user.

        Returns:
          - The username string on success.
          - ``_BOT_IDENTITY_AUTH_FAILED`` sentinel when the token is invalid
            (4xx response). Callers must treat this as a hard failure and
            abort rather than degrading to marker-only gating.
          - ``None`` for any other failure (network error, unexpected body).
        """
        if self._resolved_bot_username is not None:
            return self._resolved_bot_username
        if self.config.bot_username:
            self._resolved_bot_username = self.config.bot_username
            return self._resolved_bot_username
        resp = self.client.request("GET", "/user")
        if 400 <= resp.status_code < 500:
            # 4xx means the token is wrong — propagate as hard failure so
            # resolve_stale can abort instead of silently degrading.
            self._errors.append(
                f"GET /user: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return _BOT_IDENTITY_AUTH_FAILED
        if resp.status_code >= 500:
            self._errors.append(
                f"GET /user: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None
        data = resp.json() or {}
        username = data.get("username") or None
        if isinstance(username, str):
            self._resolved_bot_username = username
            return username
        return None

    # ------------------------------------------------------------------
    # get_last_reviewed_sha
    # ------------------------------------------------------------------
    def get_last_reviewed_sha(self) -> str | None:
        notes = self._list_summary_notes()
        if not notes:
            return None
        # sort=desc means notes[0] is the most recent.
        latest = notes[0]
        return extract_summary_sha(
            latest.get("body") or "",
            context_hint=f"gitlab_note#{latest.get('id')}",
        )

    # ------------------------------------------------------------------
    # post_summary — upsert single combined note keyed by marker
    # ------------------------------------------------------------------
    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        if not summary_body.strip():
            return SummaryResult(
                comment_id=None,
                created=False,
                updated=False,
                error="empty summary body",
            )

        marker = build_summary_marker(head_sha)
        truncated = truncate_body(summary_body, limit=_MAX_GITLAB_BODY_SIZE)
        body = (
            f"{marker}\n{truncated}\n\n---\n"
            "*AI Review — generated by "
            "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
        )

        existing = self._list_summary_notes()
        if existing:
            keep = existing[0]
            keep_id = int(keep["id"])
            resp = self.client.request(
                "PUT", self._note_url(keep_id), json_body={"body": body}
            )
            if resp.status_code >= 400:
                err = (
                    f"update summary: HTTP {resp.status_code}: {resp.text[:200]}"
                )
                self._errors.append(err)
                return SummaryResult(
                    comment_id=keep_id,
                    created=False,
                    updated=False,
                    error=err,
                )
            for dup in existing[1:]:
                dup_id = int(dup["id"])
                self.client.request("DELETE", self._note_url(dup_id))
            return SummaryResult(comment_id=keep_id, created=False, updated=True)

        resp = self.client.request(
            "POST", self._notes_url(), json_body={"body": body}
        )
        if resp.status_code >= 400:
            err = f"create summary: HTTP {resp.status_code}: {resp.text[:200]}"
            self._errors.append(err)
            return SummaryResult(
                comment_id=None, created=False, updated=False, error=err
            )
        data = resp.json() or {}
        new_id = int(data.get("id", 0)) or None
        return SummaryResult(comment_id=new_id, created=True, updated=False)

    # ------------------------------------------------------------------
    # post_skip_comment
    # ------------------------------------------------------------------
    def post_skip_comment(self, reason: str) -> SummaryResult:
        body = append_inline_marker(
            f"**AI Review skipped.** {reason.strip() or 'No changes to review.'}"
        )
        resp = self.client.request(
            "POST", self._notes_url(), json_body={"body": body}
        )
        if resp.status_code >= 400:
            err = f"skip comment: HTTP {resp.status_code}: {resp.text[:200]}"
            self._errors.append(err)
            return SummaryResult(
                comment_id=None, created=False, updated=False, error=err
            )
        data = resp.json() or {}
        new_id = int(data.get("id", 0)) or None
        return SummaryResult(comment_id=new_id, created=True, updated=False)

    # ------------------------------------------------------------------
    # advance_sha_watermark — patches sha= field in existing summary marker
    # ------------------------------------------------------------------
    def advance_sha_watermark(self, new_sha: str) -> bool:
        existing = self._list_summary_notes()
        if not existing:
            return False
        keep = existing[0]
        keep_id = int(keep["id"])
        old_body = keep.get("body") or ""
        new_body = replace_summary_sha(
            old_body, new_sha, context_hint=f"gitlab_note#{keep_id}"
        )
        if new_body == old_body:
            return False
        resp = self.client.request(
            "PUT", self._note_url(keep_id), json_body={"body": new_body}
        )
        if resp.status_code >= 400:
            self._errors.append(
                f"advance_sha: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # post_findings — inline MR discussions + body overflow in summary
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
        """Post inline discussions for eligible findings; return overflow body
        text via the body_overflow attribute on FindingsResult-style logic.

        On GitLab, the summary note already carries findings/body content (see
        `post_summary`), so this method's primary job is the inline discussions.
        Overflow is logged to errors when posting fails — the orchestrator can
        choose to retry as plain body content.
        """
        from ai_pr_review.diff.linemap import (
            parse_added_lines,
            parse_new_file_lines,
        )

        eligible_new = {(lr.file, lr.line) for lr in parse_added_lines(diff.diff_text)}
        eligible_ctx = {
            (lr.file, lr.line) for lr in parse_new_file_lines(diff.diff_text)
        }

        inline_posted = 0
        body_findings: list[Finding] = []

        for f in findings:
            if inline_posted >= max_inline:
                body_findings.append(f)
                continue
            if not is_inline_eligible(f, eligible_new):
                body_findings.append(f)
                continue
            payload = self._build_discussion_payload(
                f,
                eligible_context=eligible_ctx,
                head_sha=diff.head_sha,
                enable_suggestions=enable_suggestions,
            )
            resp = self.client.request(
                "POST", self._discussions_url(), json_body=payload
            )
            if resp.status_code < 400:
                inline_posted += 1
            else:
                # 400 commonly means position invalid for line not in MR diff.
                # Fall back to body — gl_api equivalent in bash did the same.
                self._errors.append(
                    f"discussion {f.file}:{f.line}: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                body_findings.append(f)

        # The provider returns counts; orchestrator decides what to do with
        # the body_findings list (typically: inject into the summary note via
        # post_summary on the same run before this call). We surface counts.
        if not findings:
            return FindingsResult(
                review_id=None,
                inline_posted=0,
                body_findings=0,
                event=event,
                degraded_to_comment=False,
            )

        any_failure = bool(self._errors) and inline_posted == 0 and len(findings) > 0
        return FindingsResult(
            review_id=None,
            inline_posted=inline_posted,
            body_findings=len(body_findings),
            event=event,
            degraded_to_comment=False,
            error=("all discussion posts failed" if any_failure else None),
        )

    def _build_discussion_payload(
        self,
        f: Finding,
        *,
        eligible_context: set[tuple[str, int]],
        head_sha: str,
        enable_suggestions: bool,
    ) -> dict[str, Any]:
        """Build the JSON body for POST /discussions with a position object."""
        comment = self._render_inline_comment_body(
            f, eligible_context=eligible_context, enable_suggestions=enable_suggestions
        )
        position = {
            "position_type": "text",
            "base_sha": self.config.diff_base_sha,
            "start_sha": self.config.diff_base_sha,
            "head_sha": head_sha,
            "new_path": f.file,
            "old_path": f.file,
            "new_line": f.line,
        }
        return {"body": comment, "position": position}

    def _render_inline_comment_body(
        self,
        f: Finding,
        *,
        eligible_context: set[tuple[str, int]],
        enable_suggestions: bool,
    ) -> str:
        """Render the markdown body for a GitLab inline discussion."""
        icon = severity_icon(f.severity)
        tag = format_source_tag(f)
        header = f"{icon} **[{f.severity}]** {tag} {f.finding}".strip()
        parts = [header]
        if f.remediation:
            parts.append(f"\n**Remediation:** {f.remediation}")
        if (
            enable_suggestions
            and is_suggestion_safe(f)
            and f.suggested_code
        ):
            # Multi-line suggestion uses GitLab's :-N+0 fence syntax.
            if (
                f.start_line is not None
                and f.line is not None
                and f.start_line != f.line
                and is_suggestion_range_valid(f, eligible_context=eligible_context)
            ):
                lines_above = f.line - f.start_line
                fence = f"```suggestion:-{lines_above}+0"
            else:
                fence = "```suggestion:-0+0"
            parts.append(f"\n{fence}\n{f.suggested_code}\n```")
        body = "".join(parts)
        return append_inline_marker(body)

    # ------------------------------------------------------------------
    # resolve_stale — marker-gated discussion resolution
    # ------------------------------------------------------------------
    def resolve_stale(self) -> StaleResult:
        bot_username = self._get_bot_username()
        if bot_username == _BOT_IDENTITY_AUTH_FAILED:
            # 4xx on GET /user means the token is invalid. The error is already
            # recorded in self._errors. Abort rather than silently degrading
            # the author-match check.
            return StaleResult(errors=tuple(self._errors))
        # bot_username may be None (network/parse failure) — that's acceptable
        # for marker-only gating, but we warn so operators can spot a bad PAT.
        if bot_username is None:
            self._errors.append(
                "resolve_stale: bot username unresolved; falling back to "
                "marker-only gating"
            )

        discussions = self._fetch_discussions()
        resolved = 0
        skipped_no_marker = 0
        errors: list[str] = []

        for disc in discussions:
            disc_id = disc.get("id")
            if not isinstance(disc_id, str) or not disc_id:
                continue
            notes = disc.get("notes") or []
            if not notes:
                continue
            first = notes[0]
            if not first.get("resolvable", False):
                continue
            if first.get("resolved", False):
                continue
            body = first.get("body") or ""
            author = ((first.get("author") or {}).get("username")) or None
            if not is_owned_by_us(body, author, bot_username, kind="inline"):
                skipped_no_marker += 1
                continue
            ok, status, body_snippet = self._resolve_discussion(disc_id)
            if not ok:
                errors.append(f"resolve discussion {disc_id}: HTTP {status}: {body_snippet}")
                continue
            resolved += 1

        return StaleResult(
            threads_resolved=resolved,
            reviews_dismissed=0,  # GitLab has no separate "review" entity
            threads_skipped_no_marker=skipped_no_marker,
            errors=tuple(self._errors + errors),
        )

    def _fetch_discussions(self) -> list[dict[str, Any]]:
        all_disc: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self.client.request(
                "GET",
                self._discussions_url(),
                params={"per_page": 100, "page": page},
            )
            if resp.status_code >= 400:
                self._errors.append(
                    f"fetch_discussions p{page}: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                break
            page_data = resp.json() or []
            all_disc.extend(page_data)
            if len(page_data) < 100:
                break
            page += 1
        return all_disc

    def _resolve_discussion(self, discussion_id: str) -> tuple[bool, int, str]:
        resp = self.client.request(
            "PUT",
            self._discussion_resolve_url(discussion_id),
            json_body={"resolved": True},
        )
        return resp.status_code < 400, resp.status_code, resp.text[:200]
