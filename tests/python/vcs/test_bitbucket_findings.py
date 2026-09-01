"""BitbucketProvider.post_findings — combined comment model."""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider, _strip_details_wrapper
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER_HIDDEN, SUMMARY_MARKER_PREFIX
from ai_pr_review.vcs.protocol import DiffContext


def _assert_hidden_markers_well_separated(body: str) -> None:
    """A `[//]: # (...)` reference-link marker cannot interrupt a paragraph
    per CommonMark — see test_bitbucket_summary.py's copy of this helper for
    the full rationale (#699 code review)."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("[//]:"):
            continue
        ok = i == 0 or lines[i - 1] == "" or lines[i - 1].startswith("[//]:")
        assert ok, f"hidden marker on line {i} not properly separated: {body!r}"


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> BitbucketProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    return BitbucketProvider(
        config=BitbucketConfig(
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t"
        ),
        client=client,
    )


_HEAD = "abc1234def5678abc1234def5678abc1234def56"


def _existing_summary(comment_id: int = 100, sha: str = "01d4ead4ee") -> dict:
    return {
        "id": comment_id,
        "content": {
            "raw": (
                f"<!-- ai-pr-review-summary sha={sha} -->\n"
                "## AI Review: Approved\n\n"
                "No findings yet."
            )
        },
    }


def test_post_findings_no_existing_returns_error() -> None:
    """Bitbucket requires post_summary to run first (AC5 ordering)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": []})

    prov = _make_provider(handler)
    findings = [Finding(severity="High", confidence=90, finding="x")]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert not result.ok
    assert "no summary comment" in (result.error or "")
    assert result.body_findings == 1


def test_post_findings_appends_into_existing_comment() -> None:
    captured: list[dict] = []
    existing = _existing_summary(comment_id=42)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="SQLi via string concat",
            source="blind",
            file="db.py",
            line=12,
            remediation="parameterize",
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok
    assert result.body_findings == 1
    assert result.review_id == 42

    raw = captured[0]["content"]["raw"]
    assert SUMMARY_MARKER_PREFIX in raw
    assert "## AI Review Findings" in raw
    assert "**Overall Risk:** High" in raw
    assert "SQLi via string concat" in raw
    assert "parameterize" in raw
    # Inline marker tagged on the body too — hidden form (#699): Bitbucket's
    # renderer doesn't hide raw HTML comments the way GitHub/GitLab do.
    assert INLINE_MARKER_HIDDEN in raw
    # The rendered body ends in an italic footer paragraph followed by the
    # marker — the exact shape flagged as risky in #699's code review.
    _assert_hidden_markers_well_separated(raw)


def test_post_findings_demoted_to_body_high_counts_in_headline() -> None:
    """Regression test for #622 on Bitbucket: a judge-downranked High finding
    (demoted_to_body=True) must count at its true severity in the headline —
    matching GitHub's behavior and review.outcome.classify_review_outcome's
    decision for the same finding."""
    captured: list[dict] = []
    existing = _existing_summary(comment_id=42)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="High",
            confidence=65,
            finding="author_association is not a reliable authorization check",
            source="code-reviewer",
            file=".github/workflows/ai-pr-review.yml",
            line=195,
            demoted_to_body=True,
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok

    raw = captured[0]["content"]["raw"]
    assert "**Overall Risk:** High" in raw, (
        f"demoted_to_body must not hide a High finding from the headline risk; got: {raw[:400]!r}"
    )
    assert "**Findings:** 1" in raw, (
        f"demoted_to_body must not exclude the finding from the headline count; got: {raw[:400]!r}"
    )


def test_post_findings_approve_with_only_out_of_diff_findings_still_renders_them() -> None:
    """Regression test: an all-out_of_diff finding set combined with event=APPROVE
    must NOT blank the rendered body. compute_headline() excludes genuine
    out_of_diff findings from finding_total/risk (its documented headline-count
    convention, shared with GitHub's collapsed-section logic) -- but per
    _render_combined_body's own docstring, Bitbucket has no collapsed section,
    so out_of_diff findings must still appear in the flat findings_block.
    Gating the "no findings" branch on finding_total == 0 instead of on the raw
    findings list would blank findings_block whenever every finding happens to
    be out_of_diff (always Low severity, by apply_diff_scope's invariant), so
    APPROVE + all-out_of_diff would silently drop real findings from the
    rendered body while still claiming "No findings ... look good" -- the
    exact class of bug #622 fixed, reintroduced on Bitbucket specifically by
    an incomplete first pass at this fix."""
    captured: list[dict] = []
    existing = _existing_summary(comment_id=42)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="Low",
            confidence=80,
            finding="style nit outside the diff",
            source="phpcs",
            file="legacy.php",
            line=900,
            out_of_diff=True,
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="APPROVE"
    )
    assert result.ok

    raw = captured[0]["content"]["raw"]
    assert "style nit outside the diff" in raw, (
        "an out_of_diff finding must still render in Bitbucket's flat "
        f"findings_block even when finding_total==0 triggers APPROVE; got: {raw[:400]!r}"
    )
    # The summary line must not claim "Findings: 0" directly above a rendered,
    # non-empty findings list -- that reintroduces the #622 bug one level
    # deeper: a technically-non-empty count (0) that contradicts the content
    # right below it. "0 in the diff" is the intentionally qualified wording.
    assert "**Findings:** 0 in the diff" in raw, (
        f"summary line must not read a bare 'Findings: 0' beside a rendered "
        f"out_of_diff finding; got: {raw[:400]!r}"
    )
    assert "**Findings:** 0\n" not in raw and "**Findings:** 0\n\n" not in raw, (
        f"bare 'Findings: 0' would contradict the rendered finding below it; got: {raw[:400]!r}"
    )


def test_post_findings_comment_with_only_out_of_diff_findings_does_not_contradict() -> None:
    """Same class of bug as the APPROVE case above, reachable via event=COMMENT:
    an all-out_of_diff finding set makes compute_headline() return risk="None"
    (not "Unknown", since no agents failed), so the COMMENT+Unknown+empty
    branch doesn't catch it and it falls to the generic trailing branch, which
    must also avoid a bare "Findings: 0" beside the rendered finding."""
    captured: list[dict] = []
    existing = _existing_summary(comment_id=43)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 43})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [
        Finding(
            severity="Low",
            confidence=80,
            finding="another style nit outside the diff",
            source="phpcs",
            file="legacy.php",
            line=901,
            out_of_diff=True,
        )
    ]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok

    raw = captured[0]["content"]["raw"]
    assert "another style nit outside the diff" in raw
    assert "**Findings:** 0 in the diff" in raw, (
        f"summary line must not read a bare 'Findings: 0' beside a rendered "
        f"out_of_diff finding; got: {raw[:400]!r}"
    )


def test_post_findings_approve_with_no_findings_renders_approved_block() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [_existing_summary()]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 100})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [], DiffContext(diff_text="", head_sha=_HEAD), event="APPROVE"
    )
    assert result.ok
    raw = captured[0]["content"]["raw"]
    assert "AI Review: Approved" in raw


def test_post_findings_incomplete_when_failed_agents_and_no_findings() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [_existing_summary()]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 100})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [],
        DiffContext(diff_text="", head_sha=_HEAD),
        event="COMMENT",
        failed_agents=["blind-hunter"],
    )
    assert result.ok
    raw = captured[0]["content"]["raw"]
    assert "AI Review: Incomplete" in raw
    assert "blind-hunter" in raw


def test_post_findings_put_failure_returns_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [_existing_summary()]})
        if req.method == "PUT":
            return httpx.Response(422, text="invalid")
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [Finding(severity="Low", confidence=50, finding="x")]
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event="COMMENT"
    )
    assert not result.ok
    assert "HTTP 422" in (result.error or "")


# ---------------------------------------------------------------------------
# #703: reporting.py's <details>/<summary> accordion wrapper isn't rendered
# on Bitbucket, and — because its closing </details> immediately follows the
# table with no blank line — Bitbucket's markdown table parser absorbs it
# (and everything after it, up to the next real blank line) as spurious
# trailing table rows, corrupting the table and swallowing the footer.
# ---------------------------------------------------------------------------

_SAMPLE_ACCORDION = (
    "<details>\n<summary>Token usage by agent</summary>\n\n"
    "| Agent | Model | Input | Output | Total | Est. Cost |\n"
    "|-------|-------|------:|-------:|------:|----------:|\n"
    "| code-reviewer | Sonnet 5 | 1 | 37 | 38 | $0.0415 |\n"
    "| **Total** | | **1** | **37** | **38** | **$0.0415** |"
    "\n</details>"
)


def test_strip_details_wrapper_removes_tags_and_keeps_table() -> None:
    result = _strip_details_wrapper(_SAMPLE_ACCORDION)
    assert "<details>" not in result
    assert "<summary>" not in result
    assert "</details>" not in result
    assert result.startswith("**Token usage by agent**\n\n")
    assert "| code-reviewer | Sonnet 5 |" in result


def test_strip_details_wrapper_ends_with_trailing_newline() -> None:
    """A trailing newline here becomes a real blank line at the
    "\\n".join() call site in _render_combined_body — without it, whatever
    follows the table collapses back into the exact corruption this
    function exists to prevent."""
    result = _strip_details_wrapper(_SAMPLE_ACCORDION)
    assert result.endswith("$0.0415** |\n")
    assert not result.endswith("\n\n")  # exactly one trailing newline, not two


def test_strip_details_wrapper_passes_through_unrecognized_input() -> None:
    """Anything not shaped like reporting.py's accordion (already-plain
    text, or empty) is returned unchanged rather than mangled."""
    assert _strip_details_wrapper("") == ""
    assert _strip_details_wrapper("**Already plain**\n\n| a | b |") == "**Already plain**\n\n| a | b |"


def test_post_findings_token_table_has_no_details_tags_and_separates_footer() -> None:
    """End-to-end via the real build_token_table_accordion() output (not a
    hand-built fixture) so the regex in _strip_details_wrapper is exercised
    against reporting.py's actual current format, guarding against silent
    drift between the two."""
    from pathlib import Path
    from unittest.mock import patch

    from ai_pr_review.agents.dispatch import AgentResult, TokenUsage
    from ai_pr_review.review.reporting import build_token_table_accordion

    ar = AgentResult(
        name="code-reviewer",
        output="findings output",
        token_log=TokenUsage(input=1, output=37, cache_creation=0, cache_read=0, model="claude-sonnet-5"),
        truncated=False,
    )
    sample_pricing = [
        {
            "patterns": ["claude-sonnet-5"],
            "display_name": "Sonnet 5",
            "input_rate": 3000000,
            "output_rate": 15000000,
            "cache_write_rate": 3750000,
            "cache_read_rate": 300000,
        }
    ]
    with patch("ai_pr_review.pricing.load_pricing", return_value=sample_pricing):
        token_table = build_token_table_accordion([ar], None, Path("."))
    assert "<details>" in token_table  # sanity: fixture actually needs stripping

    captured: list[dict] = []
    existing = _existing_summary(comment_id=99)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"values": [existing]})
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 99})
        return httpx.Response(404)

    prov = _make_provider(handler)
    findings = [Finding(severity="Low", confidence=60, finding="minor style nit")]
    result = prov.post_findings(
        findings,
        DiffContext(diff_text="", head_sha=_HEAD),
        event="COMMENT",
        token_table=token_table,
    )
    assert result.ok
    raw = captured[0]["content"]["raw"]

    assert "<details>" not in raw
    assert "<summary>" not in raw
    assert "</details>" not in raw
    assert "**Token usage by agent**" in raw
    # The footer must be a real blank line away from the table's last row,
    # not glued to it — otherwise Bitbucket's parser folds it into the table
    # as a garbled trailing row (the exact bug in #703's live repro).
    assert "\n\n---\n*AI Review — generated by" in raw


# ---------------------------------------------------------------------------
# Comment-clutter reduction pass: incremental-run nesting bug, truncation
# ordering, F-ID stability, and the "new since last review" marker.
# ---------------------------------------------------------------------------


def _run_post_findings_cycle(
    existing_body: str,
    findings: list[Finding],
    *,
    comment_id: int = 42,
    event: str = "REQUEST_CHANGES",
) -> str:
    """Simulate one review cycle: an existing comment with `existing_body`
    is fetched, `post_findings` renders + PUTs a new body, which is returned
    (as the caller would then feed into the next cycle as `existing_body`,
    exactly as a real incremental run reads back whatever the previous run
    wrote)."""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={"values": [{"id": comment_id, "content": {"raw": existing_body}}]},
            )
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": comment_id})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        findings, DiffContext(diff_text="", head_sha=_HEAD), event=event  # type: ignore[arg-type]
    )
    assert result.ok, result.error
    return captured[0]["content"]["raw"]


def _first_run_body(walkthrough: str, sha: str = "01d4ead4ee") -> str:
    """A comment as `post_summary` alone would have written it (#run 1,
    before `post_findings` has ever rendered a heading onto it)."""
    from ai_pr_review.vcs.marker import build_summary_marker

    marker = build_summary_marker(sha, hidden=True)
    return (
        f"{marker}\n{walkthrough}\n\n---\n"
        "*AI Review — generated by "
        "[ai-pr-review](https://github.com/tag1consulting/ai-pr-review)*"
    )


def test_incremental_runs_do_not_nest_the_prior_comment() -> None:
    """Regression for the incremental-run nesting bug: on an incremental run
    pr-summarizer is skipped, so `post_summary` never runs and the comment
    `post_findings` reads back is the FULL previous render (heading,
    findings, walkthrough, everything). Extraction must pull out only the
    true walkthrough, not the whole prior comment -- otherwise each push
    nests the last one inside the new one, growing without bound."""
    walkthrough = "Adds a new `foo.py` helper module."
    finding_a = Finding(
        severity="High", confidence=90, finding="SQLi risk", source="blind",
        file="db.py", line=12,
    )

    run1 = _run_post_findings_cycle(_first_run_body(walkthrough), [finding_a])
    # Run 1 is expected to carry the walkthrough forward under its own
    # heading exactly once.
    assert run1.count("### Summary") == 1
    assert walkthrough in run1

    run2 = _run_post_findings_cycle(run1, [finding_a])  # incremental: same finding
    run3 = _run_post_findings_cycle(run2, [finding_a])  # incremental again

    for body, label in ((run2, "run2"), (run3, "run3")):
        assert body.count("### Summary") == 1, f"{label}: {body!r}"
        assert body.count("### Findings") == 1, f"{label}: {body!r}"
        assert body.count(walkthrough) == 1, f"{label}: {body!r}"
        assert body.count("SQLi risk") == 1, f"{label}: {body!r}"

    # With an unchanged finding set, the rendered body must be stable across
    # incremental cycles -- not merely bounded, but idempotent.
    assert run2 == run3, "unchanged findings must render an identical body run-over-run"


def test_truncation_protects_findings_over_walkthrough() -> None:
    """When the 32k body limit is hit, findings must survive and the
    walkthrough is what gets cut -- the inverse of the pre-fix ordering,
    which put the (often much larger) walkthrough table first."""
    oversized_walkthrough = "x" * 40_000  # already over the 32,000-byte limit alone
    marker_finding_text = "CRITICAL_TRUNCATION_SURVIVOR_MARKER"
    finding = Finding(
        severity="Critical", confidence=95, finding=marker_finding_text,
        source="security-reviewer", file="auth.py", line=1,
    )

    raw = _run_post_findings_cycle(_first_run_body(oversized_walkthrough), [finding])

    assert len(raw.encode("utf-8")) <= 32_000 + 300  # + truncation trailer slack
    assert marker_finding_text in raw, "a Critical finding must survive truncation"
    findings_idx = raw.find("### Findings")
    assert findings_idx != -1
    # The walkthrough content, if any of it survived at all, must appear
    # after the findings section -- never take priority over it.
    walkthrough_idx = raw.find("x" * 100)
    assert walkthrough_idx == -1 or walkthrough_idx > findings_idx


def test_fingerprint_stable_and_new_finding_flagged() -> None:
    """F-IDs on Bitbucket (previously nonexistent) must stay stable across
    runs for an unchanged finding, and a finding introduced on a later run
    must be flagged as new -- while the finding that already existed must
    NOT be flagged."""
    finding_a = Finding(
        severity="Medium", confidence=80, finding="unchanged finding",
        source="code-reviewer", file="a.py", line=5,
    )
    finding_b = Finding(
        severity="High", confidence=90, finding="brand new finding",
        source="code-reviewer", file="b.py", line=9,
    )

    run1 = _run_post_findings_cycle(_first_run_body(""), [finding_a])
    # First run ever: nothing to compare against yet, so nothing is flagged
    # new even though, trivially, every finding here IS new (decision #18's
    # explicit transition-cost handling -- an all-new list on the very first
    # render carries no signal).
    assert "🆕" not in run1
    match_a1 = re.search(r"\*\*\[F(\d+)\]\*\*.*unchanged finding", run1)
    assert match_a1 is not None
    f_id_a = match_a1.group(1)

    run2 = _run_post_findings_cycle(run1, [finding_a, finding_b])

    match_a2 = re.search(r"\*\*\[F(\d+)\]\*\*.*unchanged finding", run2)
    assert match_a2 is not None
    assert match_a2.group(1) == f_id_a, "unchanged finding must keep its F-ID"

    match_b2 = re.search(r"\*\*\[F(\d+)\]\*\*.*brand new finding", run2)
    assert match_b2 is not None
    assert match_b2.group(1) != f_id_a, "new finding must get a distinct F-ID"

    # The new-since-last-review badge must land on the new finding's bullet
    # and nowhere near the unchanged one's.
    unchanged_bullet = next(
        ln for ln in run2.split("\n") if "unchanged finding" in ln
    )
    new_bullet = next(ln for ln in run2.split("\n") if "brand new finding" in ln)
    assert "🆕" not in unchanged_bullet
    assert "🆕" in new_bullet


def test_body_finding_location_links_to_bitbucket_source_browser() -> None:
    """Findings that never get an inline anchor on Bitbucket (there is no
    inline anchoring at all on this provider) still deserve a click-through
    link -- previously entirely absent. Uses Bitbucket's documented URI
    template (src/{commitish}/{filepath}#{basename}-{line}), verified against
    https://support.atlassian.com/bitbucket-cloud/docs/hyperlink-to-source-code-in-bitbucket/
    -- the line anchor is the file's basename, not its full path."""
    finding = Finding(
        severity="Low", confidence=70, finding="minor nit",
        source="code-reviewer", file="src/app.py", line=42,
    )
    raw = _run_post_findings_cycle(_first_run_body(""), [finding])

    expected_url = f"https://bitbucket.org/ws/repo/src/{_HEAD}/src/app.py#app.py-42"
    assert f"[↗]({expected_url})" in raw
    # Must append after the existing backtick-quoted location, never wrap or
    # alter it -- `_finding_ids.py`'s fingerprint-truncation literal
    # `text.find(" *(at \`")` depends on that text being undisturbed.
    assert f"*(at `src/app.py:42` [↗]({expected_url}))*" in raw


def test_body_finding_link_omits_anchor_when_line_is_none() -> None:
    """`_blob_link`'s `if line is not None:` guard must skip the
    `#basename-line` anchor entirely rather than emitting a malformed one."""
    finding = Finding(
        severity="Low", confidence=70, finding="minor nit",
        source="code-reviewer", file="src/app.py", line=None,
    )
    raw = _run_post_findings_cycle(_first_run_body(""), [finding])

    expected_url = f"https://bitbucket.org/ws/repo/src/{_HEAD}/src/app.py"
    assert f"[↗]({expected_url})" in raw
    assert "#app.py-" not in raw


def test_body_finding_link_url_encodes_path_with_special_characters() -> None:
    """A file path with a space and parentheses must be percent-encoded in
    both the path segments and the basename anchor -- previously untested
    beyond a plain alnum/slash path."""
    from urllib.parse import quote

    finding = Finding(
        severity="Low", confidence=70, finding="minor nit",
        source="code-reviewer", file="a b/file (1).py", line=3,
    )
    raw = _run_post_findings_cycle(_first_run_body(""), [finding])

    expected_path = "/".join(quote(seg) for seg in ["a b", "file (1).py"])
    expected_basename = quote("file (1).py")
    expected_url = (
        f"https://bitbucket.org/ws/repo/src/{_HEAD}/{expected_path}#{expected_basename}-3"
    )
    assert f"[↗]({expected_url})" in raw


def test_incremental_run_with_empty_prior_walkthrough_produces_no_stray_summary() -> None:
    """The "prior render exists but its walkthrough was empty" branch of
    `_extract_walkthrough` executes on run2 here (run1 has no `### Summary`
    section at all), but nothing previously asserted it behaves correctly --
    only that F-IDs stayed stable across the same two runs. Verify directly
    that no stray `### Summary` section, and no leaked findings content,
    appears."""
    finding = Finding(
        severity="Medium", confidence=80, finding="unchanged finding",
        source="code-reviewer", file="a.py", line=5,
    )
    run1 = _run_post_findings_cycle(_first_run_body(""), [finding])
    assert "### Summary" not in run1  # empty walkthrough renders no section at all

    run2 = _run_post_findings_cycle(run1, [finding])
    assert "### Summary" not in run2, (
        "an empty prior walkthrough must not leak findings/token-table "
        f"content into a stray Summary section on the next run: {run2!r}"
    )
    assert run2.count("### Findings") == 1
    assert run2.count("unchanged finding") == 1


def test_id_map_marker_dropped_when_too_large_logs_and_records_error(caplog) -> None:
    """Regression: previously this failure was completely silent -- no log,
    no recorded error -- even though the comment still posts successfully
    without F-ID stability for the cycle. `FindingsResult.ok` must stay True
    (the PUT itself succeeded); orchestrate.py's watermark/stale-cleanup
    gating on .ok/.error must not fire over a cosmetic marker loss."""
    import logging

    from ai_pr_review.vcs.marker import ID_MAP_MARKER_HIDDEN_PREFIX

    # A file path long enough that a modest number of findings blows the
    # base64-encoded id-map marker past _MAX_BITBUCKET_BODY_SIZE - _MIN_BODY_BYTES.
    long_path = "src/" + ("a" * 2000) + ".py"
    findings = [
        Finding(
            severity="Low", confidence=60, finding=f"nit {i}",
            source="code-reviewer", file=long_path, line=i + 1,
        )
        for i in range(20)
    ]
    captured: list[dict] = []
    existing_body = _first_run_body("")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200, json={"values": [{"id": 42, "content": {"raw": existing_body}}]}
            )
        if req.method == "PUT":
            import json

            captured.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404)

    prov = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.vcs.bitbucket"):
        result = prov.post_findings(
            findings, DiffContext(diff_text="", head_sha=_HEAD), event="COMMENT"
        )
    assert result.ok, "a dropped id-map marker must not fail the whole post"
    raw = captured[0]["content"]["raw"]

    assert ID_MAP_MARKER_HIDDEN_PREFIX not in raw, (
        "marker must be dropped cleanly, not corrupted into a truncated fragment"
    )
    assert any("too large" in msg for msg in caplog.messages), "must be logged, not silent"
    assert any("too large" in e for e in prov._errors), "must be recorded in provider._errors"
