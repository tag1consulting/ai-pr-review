"""Bitbucket Cloud VCS provider — ports post-review-bitbucket.sh.

Implements the VcsProvider protocol for Bitbucket Cloud REST 2.0. Marker-gated
stale cleanup; cleanup runs after a successful post (2.FR-10).

Provider differences from GitHub/GitLab:
- No separate review entity — summary + findings collapse into a single
  PR comment. There is no inline anchoring in v0.2.0; future versions may
  add it.
- Auth: HTTP Basic with email + API token (httpx.BasicAuth).
- Pagination via `next` URL in the response body (not Link header, not
  ?page=N counters).
- Comment body shape: `{"content": {"raw": "..."}}`.
- Body size limit: 32,768 chars on `content.raw`; we truncate at 32,000 to
  leave headroom for JSON encoding.
- No author info exposed uniformly on comments → marker-only ownership gating.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._body import (
    compute_headline,
    format_body_finding,
    join_findings,
    severity_icon,
    truncate_body,
)
from ai_pr_review.vcs._stale import is_owned_by_us
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
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

_MAX_BITBUCKET_BODY_SIZE: Final[int] = 32_000


@dataclass(frozen=True)
class BitbucketConfig:
    """Identifies the Bitbucket Cloud PR target and auth credentials."""

    workspace: str
    repo_slug: str
    pr_id: int
    email: str
    api_token: str
    base_url: str = "https://api.bitbucket.org/2.0"


def build_client(
    config: BitbucketConfig, retry: RetryPolicy | None = None
) -> RecordingClient:
    """Build a RecordingClient preconfigured for Bitbucket Cloud API calls."""
    http = httpx.Client(
        base_url=config.base_url,
        auth=httpx.BasicAuth(config.email, config.api_token),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    return RecordingClient(
        http=http,
        recorder=TapeRecorder.from_env(provider="bitbucket"),
        retry_policy=retry or RetryPolicy(),
    )


@dataclass
class BitbucketProvider:
    """Bitbucket Cloud REST 2.0 implementation of VcsProvider."""

    config: BitbucketConfig
    client: RecordingClient
    _errors: list[str] = field(default_factory=list, init=False, repr=False)

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------
    def _comments_url(self) -> str:
        c = self.config
        return f"/repositories/{c.workspace}/{c.repo_slug}/pullrequests/{c.pr_id}/comments"

    def _comment_url(self, comment_id: int) -> str:
        return f"{self._comments_url()}/{comment_id}"

    # ------------------------------------------------------------------
    # Pagination — Bitbucket returns a `next` URL in the body
    # ------------------------------------------------------------------
    def _list_summary_comments(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        url: str | None = self._comments_url()
        params: dict[str, Any] | None = {
            "pagelen": 100,
            "sort": "-updated_on",
        }
        # The bash version added a server-side q= filter; we apply it client-side
        # too (defensive — Bitbucket sometimes ignores q on rich-text fields).
        while url:
            resp = self.client.request("GET", url, params=params)
            if resp.status_code >= 400:
                self._errors.append(
                    f"list_summary_comments: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return results
            data = resp.json() or {}
            for item in data.get("values") or []:
                body = ((item.get("content") or {}).get("raw")) or ""
                if SUMMARY_MARKER_PREFIX in body:
                    results.append(item)
            next_url = data.get("next")
            if not isinstance(next_url, str) or not next_url:
                break
            # Strip the absolute base so the recording client's relative-URL
            # plumbing still works.
            url = _strip_base_url(next_url, self.config.base_url)
            params = None
        return results

    # ------------------------------------------------------------------
    # get_last_reviewed_sha
    # ------------------------------------------------------------------
    def get_last_reviewed_sha(self) -> str | None:
        comments = self._list_summary_comments()
        if not comments:
            return None
        # sort=-updated_on means the first match is the most recent.
        latest = comments[0]
        body = ((latest.get("content") or {}).get("raw")) or ""
        return extract_summary_sha(
            body, context_hint=f"bitbucket_comment#{latest.get('id')}"
        )

    def get_summary_body(self) -> str | None:
        comments = self._list_summary_comments()
        if not comments:
            return None
        return ((comments[0].get("content") or {}).get("raw")) or None

    # ------------------------------------------------------------------
    # post_summary / post_skip_comment / advance_sha_watermark
    # (delegated to module-level helpers for readability)
    # ------------------------------------------------------------------
    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        return _post_summary_impl(self, summary_body, head_sha)

    def post_skip_comment(self, reason: str) -> SummaryResult:
        return _post_skip_impl(self, reason)

    def advance_sha_watermark(self, new_sha: str) -> bool:
        return _advance_sha_impl(self, new_sha)

    # ------------------------------------------------------------------
    # post_findings — appends findings into the existing summary comment
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
        """On Bitbucket the findings ride inside the summary comment.

        We render the findings markdown and PUT the existing summary comment
        with the combined body. If no summary comment exists, this is a no-op
        (the orchestrator MUST call post_summary first per the AC5 ordering).

        `max_inline`, `enable_suggestions`, and `agent_prompt` are accepted for
        protocol compatibility but Bitbucket has no inline anchoring in v0.2.0.
        """
        existing = self._list_summary_comments()
        if not existing:
            err = "post_findings: no summary comment to attach findings to"
            self._errors.append(err)
            return FindingsResult(
                review_id=None,
                inline_posted=0,
                body_findings=len(findings),
                event=event,
                degraded_to_comment=False,
                error=err,
            )

        keep = existing[0]
        keep_id = int(keep["id"])
        existing_body = _comment_body(keep)

        body = _render_combined_body(
            existing_body=existing_body,
            findings=findings,
            event=event,
            failed_agents=failed_agents,
            token_table=token_table,
            agent_prompt=agent_prompt,
        )
        body = append_inline_marker(truncate_body(body, limit=_MAX_BITBUCKET_BODY_SIZE))

        resp = self.client.request(
            "PUT", self._comment_url(keep_id), json_body={"content": {"raw": body}}
        )
        if resp.status_code >= 400:
            err = f"post_findings PUT: HTTP {resp.status_code}: {resp.text[:200]}"
            self._errors.append(err)
            return FindingsResult(
                review_id=keep_id,
                inline_posted=0,
                body_findings=len(findings),
                event=event,
                degraded_to_comment=False,
                error=err,
            )
        return FindingsResult(
            review_id=keep_id,
            inline_posted=0,
            body_findings=len(findings),
            event=event,
            degraded_to_comment=False,
        )

    # ------------------------------------------------------------------
    # resolve_stale — marker-gated comment cleanup (no separate threads)
    # ------------------------------------------------------------------
    def resolve_stale(self, current_review_id: int | None = None) -> StaleResult:
        """Bitbucket has no review-thread concept; "stale" cleanup means
        deleting OLD summary-marker comments that aren't the current one
        (already handled by post_summary's duplicate cleanup).

        For belt-and-suspenders, we re-list and delete duplicates here too.
        Marker-gated via SUMMARY_MARKER_PREFIX (kind="summary").
        """
        comments = self._list_summary_comments()
        if len(comments) <= 1:
            return StaleResult()

        # Keep the most recent (first under sort=-updated_on); delete the rest
        # only if they pass the marker-gated ownership predicate.
        kept = comments[0]
        deleted = 0
        skipped = 0
        errors: list[str] = []
        for dup in comments[1:]:
            body = _comment_body(dup)
            # Bitbucket comments don't carry a uniform author field; rely on
            # the marker alone (kind="summary"). bot_login=None skips the
            # author check inside is_owned_by_us.
            if not is_owned_by_us(body, None, None, kind="summary"):
                skipped += 1
                continue
            dup_id = int(dup["id"])
            resp = self.client.request("DELETE", self._comment_url(dup_id))
            if resp.status_code < 400:
                deleted += 1
            else:
                errors.append(
                    f"delete dup #{dup_id}: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
        del kept  # explicitly retained, never deleted
        return StaleResult(
            threads_resolved=deleted,
            reviews_dismissed=0,
            threads_skipped_no_marker=skipped,
            errors=tuple(errors),
        )


def _strip_base_url(url: str, base: str) -> str:
    if url.startswith(base):
        suffix = url[len(base):]
        return suffix or "/"
    return url


def _comment_body(item: dict[str, Any]) -> str:
    return ((item.get("content") or {}).get("raw")) or ""


def _render_combined_body(
    *,
    existing_body: str,
    findings: Sequence[Finding],
    event: PostEvent,
    failed_agents: Sequence[str],
    token_table: str,
    agent_prompt: str,
) -> str:
    """Render the combined summary+findings body for Bitbucket.

    Bitbucket has no <details> rendering, so remediation is rendered as a
    flat sub-bullet (per bash post-review-bitbucket.sh:281), and (unlike
    GitHub) all findings — including genuine out_of_diff analyzer findings —
    render in the flat findings_block below since there is no collapsed
    section to redirect them to. The headline risk/count, however, use the
    shared compute_headline() helper (vcs/_body.py) so "Overall Risk" agrees
    with GitHub's headline and with review.outcome.classify_review_outcome's
    actual APPROVE/REQUEST_CHANGES decision for the same findings. Prior to
    the #622 fix this counted every finding unconditionally, which happened
    to already agree with classify_review_outcome (no exclusion bug) but
    over-counted analyzer out-of-diff findings relative to GitHub — this
    fix brings the two providers into agreement.
    """
    headline = compute_headline(findings, failed_agents)
    finding_total = headline.count
    risk = headline.risk

    # Heading + summary block.
    #
    # Gate on `not findings` (the raw list), NOT `finding_total == 0`.
    # finding_total excludes genuine out_of_diff findings (compute_headline's
    # headline-count convention, shared with GitHub's collapsed-section
    # logic) -- but per this function's own docstring, Bitbucket has no
    # collapsed section, so out_of_diff findings still render in the flat
    # findings_block below. Gating on finding_total here would blank
    # findings_block whenever every finding happens to be out_of_diff (e.g.
    # all-Low, all outside the diff), silently dropping real findings from
    # the rendered body while still claiming "no findings" -- reproducing
    # the exact class of bug #622 fixed, on Bitbucket specifically. Using
    # the raw `findings` list keeps this gate about whether there is
    # anything to render, not about the (deliberately narrower) headline
    # count.
    if event == "APPROVE" and not findings:
        heading = "## AI Review: Approved"
        summary_block = (
            "No findings above the confidence threshold. The changes look good."
        )
        findings_block = ""
    elif event == "COMMENT" and risk == "Unknown" and not findings:
        heading = "## AI Review: Incomplete"
        joined = ", ".join(failed_agents)
        summary_block = (
            "No findings above the confidence threshold, but one or more "
            f"agents failed: {joined}\n\n"
            "The review may be incomplete. Please verify manually or re-run "
            "the review."
        )
        findings_block = ""
    elif event == "APPROVE":
        heading = "## AI Review: Approved"
        summary_block = (
            f"{severity_icon(risk)} **Overall Risk:** {risk} | "
            f"**Findings:** {finding_total}\n\n"
            "No Critical or High findings. The changes look good — Medium/Low "
            "findings are informational only."
        )
        findings_block = "### Findings (informational)\n" + join_findings(
            format_body_finding(f) for f in findings
        )
    else:
        heading = "## AI Review Findings"
        summary_block = (
            f"{severity_icon(risk)} **Overall Risk:** {risk} | "
            f"**Findings:** {finding_total}"
        )
        findings_block = "### Findings\n" + join_findings(
            format_body_finding(f) for f in findings
        )

    # Preserve the marker line + the user-supplied summary text from the
    # existing comment by stripping the heading-onward part. The marker line
    # is the first line of the existing body; everything after it up to the
    # bash-style "---" footer is the original summary.
    head_lines = existing_body.split("\n", 1)
    marker_line = head_lines[0] if head_lines else ""
    original_summary_text = ""
    if len(head_lines) > 1:
        rest = head_lines[1]
        # Strip footer if present
        if "\n---\n*AI Review" in rest:
            rest = rest.split("\n---\n*AI Review", 1)[0]
        original_summary_text = rest.strip()

    pr_summary_block = ""
    if original_summary_text:
        # Drop any prior "## AI Review*" heading so we don't duplicate it
        lines = [
            ln for ln in original_summary_text.split("\n") if not ln.startswith("## AI Review")
        ]
        original_summary_text = "\n".join(lines).strip()
        if original_summary_text:
            pr_summary_block = f"\n### Summary\n{original_summary_text}\n"

    parts: list[str] = [marker_line, heading, "", summary_block]
    if pr_summary_block:
        parts.append(pr_summary_block)
    if findings_block:
        parts.append(findings_block)
    if token_table:
        parts.append(token_table)
    if agent_prompt and findings:
        parts.append(agent_prompt)
    parts.append(
        "---\n*AI Review — generated by "
        "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Summary upsert + skip + watermark advance
# ---------------------------------------------------------------------------


def _post_summary_impl(
    provider: BitbucketProvider, summary_body: str, head_sha: str
) -> SummaryResult:
    if not summary_body.strip():
        return SummaryResult(
            comment_id=None, created=False, updated=False, error="empty summary body"
        )

    marker = build_summary_marker(head_sha)
    truncated = truncate_body(summary_body, limit=_MAX_BITBUCKET_BODY_SIZE)
    body = (
        f"{marker}\n{truncated}\n\n---\n"
        "*AI Review — generated by "
        "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
    )
    payload = {"content": {"raw": body}}

    existing = provider._list_summary_comments()
    if existing:
        keep = existing[0]
        keep_id = int(keep["id"])
        resp = provider.client.request(
            "PUT", provider._comment_url(keep_id), json_body=payload
        )
        if resp.status_code >= 400:
            err = f"update summary: HTTP {resp.status_code}: {resp.text[:200]}"
            provider._errors.append(err)
            return SummaryResult(
                comment_id=keep_id, created=False, updated=False, error=err
            )
        for dup in existing[1:]:
            dup_id = int(dup["id"])
            provider.client.request("DELETE", provider._comment_url(dup_id))
        return SummaryResult(comment_id=keep_id, created=False, updated=True)

    resp = provider.client.request("POST", provider._comments_url(), json_body=payload)
    if resp.status_code >= 400:
        err = f"create summary: HTTP {resp.status_code}: {resp.text[:200]}"
        provider._errors.append(err)
        return SummaryResult(comment_id=None, created=False, updated=False, error=err)
    data = resp.json() or {}
    new_id = int(data.get("id", 0)) or None
    return SummaryResult(comment_id=new_id, created=True, updated=False)


def _list_skip_comments_bb(provider: BitbucketProvider) -> list[dict[str, Any]]:
    """Return all PR comments whose body contains SKIP_MARKER."""
    results: list[dict[str, Any]] = []
    url: str | None = provider._comments_url()
    params: dict[str, Any] | None = {"pagelen": 100, "sort": "-updated_on"}
    while url:
        resp = provider.client.request("GET", url, params=params)
        if resp.status_code >= 400:
            provider._errors.append(
                f"list_skip_comments: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return results
        data = resp.json() or {}
        for item in data.get("values") or []:
            body = _comment_body(item)
            if has_skip_marker(body):
                results.append(item)
        next_url = data.get("next")
        if not isinstance(next_url, str) or not next_url:
            break
        url = _strip_base_url(next_url, provider.config.base_url)
        params = None
    return results


def _post_skip_impl(provider: BitbucketProvider, reason: str) -> SummaryResult:
    raw = append_skip_marker(
        f"**AI Review skipped.** {reason.strip() or 'No changes to review.'}"
    )
    existing = _list_skip_comments_bb(provider)
    if existing:
        keep = existing[0]
        keep_id = int(keep["id"])
        resp = provider.client.request(
            "PUT", provider._comment_url(keep_id), json_body={"content": {"raw": raw}}
        )
        if resp.status_code >= 400:
            err = f"update skip comment: HTTP {resp.status_code}: {resp.text[:200]}"
            provider._errors.append(err)
            return SummaryResult(
                comment_id=keep_id, created=False, updated=False, error=err
            )
        for dup in existing[1:]:
            dup_id = int(dup["id"])
            provider.client.request("DELETE", provider._comment_url(dup_id))
        return SummaryResult(comment_id=keep_id, created=False, updated=True)

    resp = provider.client.request(
        "POST", provider._comments_url(), json_body={"content": {"raw": raw}}
    )
    if resp.status_code >= 400:
        err = f"skip comment: HTTP {resp.status_code}: {resp.text[:200]}"
        provider._errors.append(err)
        return SummaryResult(comment_id=None, created=False, updated=False, error=err)
    data = resp.json() or {}
    new_id = int(data.get("id", 0)) or None
    return SummaryResult(comment_id=new_id, created=True, updated=False)


def _advance_sha_impl(provider: BitbucketProvider, new_sha: str) -> bool:
    existing = provider._list_summary_comments()
    if not existing:
        return False
    keep = existing[0]
    keep_id = int(keep["id"])
    old_body = _comment_body(keep)
    new_body = replace_summary_sha(
        old_body, new_sha, context_hint=f"bitbucket_comment#{keep_id}"
    )
    if new_body == old_body:
        return False
    resp = provider.client.request(
        "PUT",
        provider._comment_url(keep_id),
        json_body={"content": {"raw": new_body}},
    )
    if resp.status_code >= 400:
        provider._errors.append(
            f"advance_sha: HTTP {resp.status_code}: {resp.text[:200]}"
        )
        return False
    return True
