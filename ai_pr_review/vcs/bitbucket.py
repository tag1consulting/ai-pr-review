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

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import quote

import httpx

from ai_pr_review.findings.models import Finding, Severity
from ai_pr_review.vcs._body import (
    compute_headline,
    format_body_finding,
    join_findings,
    severity_icon,
    truncate_body,
)
from ai_pr_review.vcs._finding_ids import assemble_id_map, fingerprint
from ai_pr_review.vcs._stale import is_owned_by_us
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
    INLINE_MARKER_HIDDEN,
    SKIP_MARKER_HIDDEN,
    SUMMARY_MARKER_HIDDEN_PREFIX,
    SUMMARY_MARKER_PREFIX,
    _hidden_marker_separator,
    append_inline_marker,
    append_skip_marker,
    build_id_map_marker,
    build_summary_marker,
    extract_id_map,
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

_log = logging.getLogger(__name__)

_MAX_BITBUCKET_BODY_SIZE: Final[int] = 32_000
# Mirrors github.py's floor: never let the id-map marker crowd the visible
# body down to nothing -- if it would, drop the marker for this cycle instead
# (F-ID stability degrades gracefully rather than the review going blank).
_MIN_BODY_BYTES: Final[int] = 4_096

_SEVERITY_RANK: Final[dict[Severity, int]] = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
}


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
                if SUMMARY_MARKER_PREFIX in body or SUMMARY_MARKER_HIDDEN_PREFIX in body:
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

        # Stable F-IDs and a "new since last review" signal, mirroring
        # GitHub's assemble_id_map/fingerprint machinery -- Bitbucket already
        # fetched existing_body above, so it's reused directly as the sole
        # prior body rather than issuing a second lookup via
        # get_summary_body() (same data, one fewer HTTP round-trip).
        prior_id_map = extract_id_map(existing_body) if existing_body else {}
        id_map = assemble_id_map(
            [existing_body] if existing_body else [], list(findings)
        )

        body = _render_combined_body(
            existing_body=existing_body,
            findings=findings,
            event=event,
            failed_agents=failed_agents,
            token_table=token_table,
            agent_prompt=agent_prompt,
            id_map=id_map,
            prior_id_map=prior_id_map,
            workspace=self.config.workspace,
            repo_slug=self.config.repo_slug,
            head_sha=diff.head_sha,
        )

        # Embed the ID map as a hidden marker so the next run (and the
        # dismiss workflow, once Bitbucket gets one) can reconstruct
        # fingerprint -> F-ID associations without re-parsing rendered bullet
        # text. Reserve its bytes before truncating -- same ordering as
        # github.py's post_findings -- so a near-the-limit body doesn't
        # silently drop the marker and renumber every F-ID next cycle.
        id_map_marker = ""
        try:
            id_map_marker = build_id_map_marker(id_map, hidden=True)
        except Exception as exc:  # noqa: BLE001
            # Logged (not just appended to self._errors): orchestrate.py never
            # reads this provider's _errors list for a normal review run (only
            # slash/dismiss.py's command handlers do), and this failure must
            # not set FindingsResult.error either -- the comment itself still
            # posts fine below, just without F-ID stability for this cycle,
            # and orchestrate.py gates watermark-advance/stale-cleanup on
            # .ok/.error (#493) -- failing that gate over a cosmetic marker
            # loss would force an unnecessary full re-diff next cycle, a worse
            # outcome than the degraded F-IDs this is actually about. Mirrors
            # github.py's identical failure-class handling.
            _log.warning("bitbucket: failed to build id-map marker: %s", exc)
            self._errors.append(f"post_findings: failed to build id-map marker: {exc}")
        marker_bytes = len(id_map_marker.encode("utf-8")) if id_map_marker else 0
        # +2, not +1: the separator that will actually be prepended below is
        # `_hidden_marker_separator(body)`, and at this call site `body` (the
        # freshly-appended INLINE_MARKER_HIDDEN form) never ends in a
        # newline, so that separator is always the 2-byte "\n\n" -- never the
        # 1-byte "\n" case that function also supports for other callers.
        marker_reserve = marker_bytes + 2 if id_map_marker else 0
        if id_map_marker and marker_reserve > _MAX_BITBUCKET_BODY_SIZE - _MIN_BODY_BYTES:
            _log.warning(
                "bitbucket: id-map marker (%d bytes) too large to fit in "
                "comment body for %s/%s PR #%s; omitting marker for this "
                "cycle -- F-ID stability may degrade",
                marker_bytes,
                self.config.workspace, self.config.repo_slug, self.config.pr_id,
            )
            self._errors.append(
                f"post_findings: id-map marker ({marker_bytes} bytes) too large "
                "to fit in comment body; omitting for this cycle"
            )
            id_map_marker = ""
            marker_reserve = 0
        truncate_limit = max(0, _MAX_BITBUCKET_BODY_SIZE - marker_reserve)
        body = append_inline_marker(
            truncate_body(body, limit=truncate_limit),
            marker=INLINE_MARKER_HIDDEN,
        )
        if id_map_marker:
            # `[//]: # (...)` cannot interrupt a paragraph per CommonMark
            # (see marker.py's _hidden_marker_separator docstring) — a bare
            # "\n" after the footer's italic line would risk it rendering as
            # literal text on a stricter renderer than Bitbucket's own.
            body += _hidden_marker_separator(body) + id_map_marker

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


# reporting.py's build_token_table_accordion() wraps the token-cost table in
# raw <details>/<summary> for GitHub/GitLab's native collapsible rendering.
# Bitbucket has neither: it escapes the tags to literal text, and — because
# the wrapper's closing </details> immediately follows the table with no
# blank line — its markdown table parser then absorbs that line (and
# whatever comes after it, up to the next real blank line) as spurious
# trailing table rows, corrupting the table and swallowing the footer that
# follows it in _render_combined_body (#703).
_DETAILS_WRAPPER_RE = re.compile(r"\A<details>\n<summary>(.*?)</summary>\n\n(.*)\n</details>\Z", re.DOTALL)


def _strip_details_wrapper(token_table_md: str) -> str:
    """Convert a reporting.py accordion string to Bitbucket-safe markdown.

    Replaces the <details>/<summary> wrapper with a plain bold heading, and
    always returns a string ending in a newline: _render_combined_body joins
    parts with "\\n", so a single trailing newline here becomes a real blank
    line at the join point, keeping whatever follows out of the table.

    Falls back to returning the input unchanged if it doesn't match the
    expected wrapper shape (e.g. already-plain text) rather than mangling
    something this function doesn't recognize.
    """
    match = _DETAILS_WRAPPER_RE.match(token_table_md)
    if not match:
        return token_table_md
    heading, table = match.group(1), match.group(2)
    return f"**{heading}**\n\n{table}\n"


def _blob_link(
    *, workspace: str, repo_slug: str, head_sha: str, file: str, line: int | None
) -> str:
    """Build a Bitbucket Cloud source-browser URL, anchored to a line.

    Per Atlassian's documented URI template (``src/{commitish}/{filepath}
    #{fileline}``), the line-anchor segment is the file's *basename* plus the
    line number, joined by a dash — NOT the full path (verified 2026-09-01
    against https://support.atlassian.com/bitbucket-cloud/docs/hyperlink
    -to-source-code-in-bitbucket/; a `#lines-N` scheme, closer to GitHub's,
    would have silently produced a dead anchor).
    """
    quoted_path = "/".join(quote(seg) for seg in file.split("/"))
    url = f"https://bitbucket.org/{workspace}/{repo_slug}/src/{head_sha}/{quoted_path}"
    if line is not None:
        basename = quote(file.rsplit("/", 1)[-1])
        url += f"#{basename}-{line}"
    return url


def _render_combined_body(
    *,
    existing_body: str,
    findings: Sequence[Finding],
    event: PostEvent,
    failed_agents: Sequence[str],
    token_table: str,
    agent_prompt: str,
    id_map: dict[str, int],
    prior_id_map: dict[str, int],
    workspace: str,
    repo_slug: str,
    head_sha: str,
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

    ``id_map``/``prior_id_map`` carry stable F-IDs and the "new since the
    prior review" signal (see module docstring / decisions #17-18 in the
    PR-comment-clutter plan): Bitbucket rebuilds the whole findings list on
    every run rather than only notifying on genuinely-new ones the way
    GitHub does, so findings are sorted new-first then by severity, and a
    finding absent from ``prior_id_map`` is flagged inline. ``prior_id_map``
    is empty both on a genuine first run and whenever no marker could be
    parsed from ``existing_body`` — in either case nothing is flagged new,
    since an all-new list on the very first render would carry no signal.
    """
    headline = compute_headline(findings, failed_agents)
    finding_total = headline.count
    risk = headline.risk

    has_prior_id_map = bool(prior_id_map)

    def _is_new(f: Finding) -> bool:
        return has_prior_id_map and fingerprint(f) not in prior_id_map

    sorted_findings = sorted(
        findings,
        key=lambda f: (0 if _is_new(f) else 1, _SEVERITY_RANK.get(f.severity, 99)),
    )

    def _render_bullet(f: Finding) -> str:
        loc_note = ""
        if f.file:
            url = _blob_link(
                workspace=workspace, repo_slug=repo_slug, head_sha=head_sha,
                file=f.file, line=f.line,
            )
            loc_note = f" [↗]({url})"
        bullet = format_body_finding(
            f, location_note=loc_note, finding_id=id_map.get(fingerprint(f))
        )
        if _is_new(f):
            # Prepend after the leading "- " so the line still opens with a
            # valid Markdown list marker.
            bullet = f"- 🆕 {bullet[2:]}"
        return bullet

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
    elif event == "APPROVE" and finding_total == 0:
        # findings is non-empty here (the `not findings` branch above already
        # caught the empty case) but every finding is out_of_diff, so
        # compute_headline()'s deliberately narrower count reads 0. Since
        # Bitbucket has no collapsed section to hide those findings in (see
        # this function's docstring), findings_block below still renders them
        # — say so explicitly rather than printing "Findings: 0" directly
        # above a non-empty list.
        heading = "## AI Review: Approved"
        summary_block = (
            f"{severity_icon(risk)} **Overall Risk:** {risk} | "
            "**Findings:** 0 in the diff\n\n"
            "No Critical or High findings in the diff. The changes look good "
            "— findings below are pre-existing issues on unchanged lines."
        )
        findings_block = "### Findings (informational)\n" + join_findings(
            _render_bullet(f) for f in sorted_findings
        )
    elif event == "APPROVE":
        heading = "## AI Review: Approved"
        summary_block = (
            f"{severity_icon(risk)} **Overall Risk:** {risk} | "
            f"**Findings:** {finding_total}\n\n"
            "No Critical or High findings. The changes look good — Medium/Low "
            "findings are informational only."
        )
        findings_block = "### Findings (informational)\n" + join_findings(
            _render_bullet(f) for f in sorted_findings
        )
    elif finding_total == 0:
        # Reachable for event == "COMMENT" with a non-empty, all-out_of_diff
        # finding set (risk == "None", not "Unknown", so the branch above
        # doesn't catch it) — same rationale as the APPROVE+finding_total==0
        # branch above: don't print "Findings: 0" beside a rendered list.
        heading = "## AI Review Findings"
        summary_block = (
            f"{severity_icon(risk)} **Overall Risk:** {risk} | "
            "**Findings:** 0 in the diff"
        )
        findings_block = "### Findings\n" + join_findings(
            _render_bullet(f) for f in sorted_findings
        )
    else:
        heading = "## AI Review Findings"
        summary_block = (
            f"{severity_icon(risk)} **Overall Risk:** {risk} | "
            f"**Findings:** {finding_total}"
        )
        findings_block = "### Findings\n" + join_findings(
            _render_bullet(f) for f in sorted_findings
        )

    # Preserve the marker line + the walkthrough/summary text from the
    # existing comment. The marker line is always the first line of the
    # existing body. `_extract_walkthrough` isolates only the true
    # walkthrough content -- see its docstring for why a naive "everything
    # after the heading" extraction re-nests the whole prior comment on
    # every incremental run (the bug this fixes).
    head_lines = existing_body.split("\n", 1)
    marker_line = head_lines[0] if head_lines else ""
    original_summary_text = _extract_walkthrough(existing_body)

    pr_summary_block = f"\n### Summary\n{original_summary_text}\n" if original_summary_text else ""

    # Findings and the token table before the walkthrough: if the body is
    # truncated at the byte limit, the actionable content and the (always
    # small) cost table survive and the walkthrough is what gets cut, not
    # the other way around. Findings used to sit after a potentially-large
    # walkthrough and could be silently guillotined; the token table had the
    # same bug (#728) -- a long carried-forward walkthrough could push the
    # whole body past the byte limit and truncate away the trailing token
    # table (and with it the model name), even though the table itself is
    # only a few hundred bytes. `_extract_walkthrough` below is unaffected by
    # this ordering: it splits on "### Summary" and only looks at what comes
    # *after* that heading, never at what precedes it.
    parts: list[str] = [marker_line, heading, "", summary_block]
    if findings_block:
        parts.append(findings_block)
    if token_table:
        parts.append(_strip_details_wrapper(token_table))
    if pr_summary_block:
        parts.append(pr_summary_block)
    if agent_prompt and findings:
        parts.append(agent_prompt)
    parts.append(
        "---\n*AI Review — generated by "
        "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
    )
    return "\n".join(parts)


# Section-boundary needles `_extract_walkthrough` cuts the carried-forward
# walkthrough text at. All four can legitimately follow the "### Summary"
# heading in a body this function itself rendered: "### Findings" and
# "### Findings (informational)" (findings_block; both variants start with
# the shorter needle), the token table in either its Bitbucket-stripped bold
# form or (defensively) the raw <details> form if _strip_details_wrapper ever
# fails to match, and the agent-prompt <details> block (unused by any current
# caller -- see ai_pr_review/vcs/protocol.py -- but cheap to guard against a
# future one).
_WALKTHROUGH_BOUNDARIES: Final[tuple[str, ...]] = (
    "\n### Findings",
    "\n**Token usage by agent**",
    "\n<details>\n<summary>Token usage by agent</summary>",
    "\n<details>\n<summary>🤖 Prompt for AI agents</summary>",
)


def _extract_walkthrough(existing_body: str) -> str:
    """Return only the walkthrough/summary text carried forward from a prior
    rendered comment, discarding findings, token table, and agent-prompt
    content that may also be present.

    On the very first cycle, `existing_body` was written by `post_summary`
    alone and contains nothing but the marker line and the raw pr-summarizer
    output (no "## AI Review" heading has ever been rendered yet) -- the
    whole thing, footer-stripped, IS the walkthrough.

    On every later cycle, `existing_body` is whatever `_render_combined_body`
    rendered last time, which always carries a "## AI Review*" heading. If
    that render also had a non-empty walkthrough, it lives under its own
    "### Summary" heading (written by this same function, below); this
    function must extract ONLY that subsection, stopping at whichever
    section boundary comes next. Falling back to "everything after the
    heading" here is exactly the bug this exists to prevent: on an
    incremental run (pr-summarizer skipped, so `post_summary` is a no-op),
    `existing_body` is the FULL prior render -- findings, token table, and
    all -- and treating all of that as "the walkthrough" nests the entire
    previous comment inside the new one, every push.
    """
    head_lines = existing_body.split("\n", 1)
    if len(head_lines) < 2:
        return ""
    rest = head_lines[1]
    if "\n---\n*AI Review" in rest:
        rest = rest.split("\n---\n*AI Review", 1)[0]

    if not rest.startswith("## AI Review"):
        # Anchored to the start of `rest`, not "appears anywhere" -- a
        # walkthrough carried forward from a genuine first run could contain
        # an LLM-authored line that happens to start with "## AI Review"
        # further down (steerable by the diff content it summarizes); only
        # the very first line reliably indicates a prior post_findings
        # render, since that heading is always what `_render_combined_body`
        # writes immediately after the marker line.
        return rest.strip()

    if "\n### Summary\n" not in rest:
        # A prior render exists but its walkthrough was empty -- propagate
        # that emptiness rather than swallowing the findings/token-table
        # content that actually follows the heading in this body.
        return ""
    rest = rest.split("\n### Summary\n", 1)[1]
    cut = min(
        (idx for idx in (rest.find(b) for b in _WALKTHROUGH_BOUNDARIES) if idx != -1),
        default=-1,
    )
    if cut != -1:
        rest = rest[:cut]
    return rest.strip()


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

    marker = build_summary_marker(head_sha, hidden=True)
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
        f"**AI Review skipped.** {reason.strip() or 'No changes to review.'}",
        inline_marker=INLINE_MARKER_HIDDEN,
        skip_marker=SKIP_MARKER_HIDDEN,
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
