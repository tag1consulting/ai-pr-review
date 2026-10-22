"""Body-formatting helpers shared across VCS providers.

Severity icon, display-only risk, source tag, body finding, body truncation,
and agent-prompt helpers.
The review-outcome classification proper lives in
`ai_pr_review.review.outcome` (E2.S6); this module only formats for display.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ai_pr_review.findings.models import MAX_ALSO_REPORTED, Finding, split_also_reported
from ai_pr_review.output_clamp import (
    clamp_active,
    clamp_repo,
    neutralize_references,
    strict_text,
)
from ai_pr_review.vcs.marker import HIDDEN_MARKER_OPENER_RE

# GitHub's body size limit (bytes). GitLab/Bitbucket have similar but slightly
# different limits — each provider can override.
GITHUB_MAX_BODY_SIZE: Final[int] = 65_536

# Substring of `truncate_body`'s trailer, exported so callers (e.g.
# `ai_pr_review.slash.github_orchestration.classify_finding`) can detect a
# truncated body without duplicating the trailer text. A truncated body's id-map marker can
# still list an ID whose bullet was cut off by the truncation itself, so a
# caller reconstructing "which bucket is this ID in" from a truncated body
# must not trust the absence of a bullet as proof the ID isn't a body finding.
TRUNCATION_MARKER: Final[str] = "Review output truncated"

_SEVERITY_ICONS: Final[dict[str, str]] = {
    "critical": "⛔",
    "high": "🚨",
    "medium": "🔶",
    "low": "💬",
    "none": "✅",
    "unknown": "❔",
}


def severity_icon(severity: str) -> str:
    return _SEVERITY_ICONS.get(severity.lower(), "💬")


# Published docs site (docs/_config.yml). Lives here, not in slash/handlers.py,
# so vcs/ renderers can link to it without importing slash/.
SLASH_COMMANDS_DOC_URL: Final[str] = (
    "https://tag1consulting.github.io/ai-pr-review/slash-commands"
)

_ERROR_BODY_LIMIT: Final[int] = 500
# A scope list is kept whole up to this many characters, so an odd body cannot
# make one error string unbounded.
_SCOPE_LIST_LIMIT: Final[int] = 2000


def _join_scopes(scopes: list[str]) -> str:
    joined = ", ".join(scopes) or "none"
    if len(joined) > _SCOPE_LIST_LIMIT:
        return joined[:_SCOPE_LIST_LIMIT] + " ..."
    return joined


def _scope_lists(body: object) -> tuple[list[str], list[str]] | None:
    """Pull ``(required, granted)`` scope lists out of a Bitbucket error body.

    Bitbucket answers a call that lacks a scope with
    ``{"error": {"message": ..., "detail": {"required": [...], "granted": [...]}}}``.
    Returns None for any other shape.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    detail = error.get("detail") if isinstance(error, dict) else None
    if not isinstance(detail, dict):
        return None
    required, granted = detail.get("required"), detail.get("granted")
    if not isinstance(required, list) or not isinstance(granted, list):
        return None
    return [str(x) for x in required], [str(x) for x in granted]


def format_http_error_body(text: str, *, limit: int = _ERROR_BODY_LIMIT) -> str:
    """Format an HTTP error response body for an error message or log line.

    A Bitbucket scope error lists the scopes the call needed and the scopes the
    token has. A fixed-length cut used to drop the second list, which is the one
    an operator needs. This puts the message and both lists first and keeps them
    whole. Any other body is cut at ``limit`` characters.
    """
    try:
        body = json.loads(text)
    except (ValueError, RecursionError):
        # RecursionError: a body that is nested deeper than the parser allows.
        body = None
    scopes = _scope_lists(body)
    if scopes is not None:
        required, granted = scopes
        error = body["error"]  # _scope_lists proved this is a dict
        message = str(error.get("message") or "missing scopes")
        return (
            f"{message[:limit]} (required scopes: {_join_scopes(required)}. "
            f"granted scopes: {_join_scopes(granted)})"
        )
    return text[:limit]


_LEADING_SUMMARY_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"\A\s*#{1,6}[ \t]*\**[ \t]*summary[ \t]*:?[ \t]*\**[ \t]*#*[ \t]*(?:\n|\Z)",
    re.IGNORECASE,
)


def strip_leading_summary_heading(text: str) -> str:
    """Drop one leading ``Summary`` heading line (any level) from ``text``.

    The pr-summarizer prompt makes the model open with ``## Summary``, and
    the renderers wrap that text under their own ``### Summary`` heading, so
    the comment would show two. Strips at most one heading, only when it is
    the first non-blank line and its text is exactly "Summary".
    """
    return _LEADING_SUMMARY_HEADING_RE.sub("", text, count=1).lstrip("\n")


@dataclass(frozen=True)
class Headline:
    """Review-body headline: the "Overall Risk | Findings: N" line's inputs.

    Shared by every VCS renderer so "Overall Risk" can never disagree with
    itself across providers, and can never contradict the true severity of a
    finding just because that finding was judge-downranked or diff-scoped.
    Fixes tag1consulting/ai-pr-review#622 (GitHub silently excluded
    judge-downranked findings from this calculation via a stale out_of_diff
    filter; Bitbucket over-counted the opposite way by not filtering
    analyzer out-of-diff findings at all).
    """

    risk: str
    count: int


def compute_headline(
    findings: Sequence[Finding], failed_agents: Sequence[str]
) -> Headline:
    """Compute the headline risk label and count for a review body.

    Excludes only genuine ``out_of_diff`` findings (native-analyzer findings
    outside the changed-line set, always capped to Low severity by
    ``apply_diff_scope`` — see ``findings/models.py``'s field docs for the
    invariant). Judge-downranked findings (``demoted_to_body=True``) are
    NEVER excluded here regardless of severity: downrank changes where a
    finding is rendered (inline vs. body), not whether it counts toward risk.

    Note the actual, narrower invariant with ``review.outcome
    .classify_review_outcome`` (which computes the real REQUEST_CHANGES
    /APPROVE decision, with no ``out_of_diff`` filtering of its own): this
    function's risk never ranks *below* that decision's risk for the same
    findings — never a silent understatement. The two functions do NOT
    always agree outright, since ``classify_review_outcome`` counts every
    finding (including ``out_of_diff``) while this function excludes
    ``out_of_diff`` findings from the headline by design; for an
    out_of_diff-only, all-Low finding set the two diverge (headline: None/0;
    outcome: Low/APPROVE) without violating the never-understate invariant,
    since neither one requests changes in that case. A combinatorial test in
    tests/python/vcs/test_body.py (test_compute_headline_never_disagrees
    _with_classify_review_outcome) covers the ``demoted_to_body`` axis this
    invariant actually depends on; it deliberately excludes the
    out_of_diff-mismatched-severity axis, since that state is never
    constructed by production code (see the model-level guard test
    alongside it).
    """
    in_headline = [f for f in findings if not f.out_of_diff]
    count = len(in_headline)

    if count == 0:
        risk = "None" if not failed_agents else "Unknown"
    else:
        risk = "Low"
        for level in ("Critical", "High", "Medium", "Low"):
            if any(f.severity == level for f in in_headline):
                risk = level
                break

    return Headline(risk=risk, count=count)


# HTML control sequences that, when smuggled into a finding via prompt
# injection, would break out of the rendered review structure: <details>/
# <summary> tags can collapse and hide sibling findings from a human reviewer,
# and HTML comment markers can comment out following content. The VCS markdown
# renderers strip dangerous HTML (scripts/handlers), so this is an integrity
# concern, not XSS — but for a security-review tool, hiding flagged findings is
# itself a problem. We defang only these structural sequences (leaving benign
# markdown like code spans, lists, and emphasis intact) by inserting a
# zero-width space after the opening angle-bracket / first dash.
_DEFANG_SEQUENCES: Final[tuple[tuple[str, str], ...]] = (
    ("<details", "<​details"),
    ("</details", "<​/details"),
    ("<summary", "<​summary"),
    ("</summary", "<​/summary"),
    ("<!--", "<​!--"),
    ("-->", "--​>"),
    # issue #914: the bot's own bullet-rendering syntax -- **[F<n>]** (the
    # finding-ID token) and *(at `file:line`)* (the location suffix) -- is
    # otherwise unsanitized in finding/remediation text. A prompt-injected
    # finding could embed a forged bullet using this exact syntax, which the
    # id-recovery scanners in vcs/_finding_ids.py and
    # slash/github_orchestration.py (both first-match, per-line) would parse
    # as a second, real finding -- shadowing or redirecting a maintainer's
    # dismiss/false-positive/wont-fix F<n> command. `__[F`/`__(at ` (the
    # double-underscore bold variant GitHub Flavored Markdown renders
    # identically to `**`) are defanged too so a human reviewer can't be
    # visually misled either, even though the bot's own regex parser
    # (_ID_RE, requiring literal `**`) was never fooled by that variant.
    ("**[F", "**​[F"),
    ("__[F", "__​[F"),
    ("*(at `", "*(​at `"),
    ("__(at `", "__​(at `"),
)

# Needles matched case-insensitively rather than via a plain str.replace.
# _ID_RE (vcs/_finding_ids.py) matches **[F<n>]** with re.IGNORECASE, so a
# prompt-injected "**[f2]**" (lowercase) would survive a case-sensitive
# defang untouched and still parse as a real F-ID token (issue #914 found
# during this fix's own review pass -- the bot only ever emits uppercase F,
# but the parser's leniency means the defang has to match it).
_CASE_INSENSITIVE_NEEDLES: Final[frozenset[str]] = frozenset(
    {"<details", "</details", "<summary", "</summary", "**[F", "__[F"}
)


def sanitize_display_text(text: str, *, strict: bool | None = None) -> str:
    """Neutralize structure-breaking HTML (and this repo's own comment-marker
    syntax) in LLM-derived display text.

    Story 17-11 (CAP-5): on every run it also breaks ``@mention`` and
    ``owner/repo#N`` references with a zero-width space. With ``strict`` true
    (the default inside a fork run's ``output_clamp`` scope) it also removes
    images, links to anywhere but this repository, raw HTML that links or
    loads, and autolinked URLs, and drops invisible characters.

    Applied to ``finding`` and ``remediation`` strings (which can be steered by
    prompt injection in PR content) before they are interpolated into a posted
    comment body, and (#886) to the full pr-summarizer/issue-linker output
    before it becomes part of a posted summary comment -- both are read back
    verbatim on the next run by ``marker.py``'s ``extract_*`` functions, so
    any text this function doesn't cover is a potential forgery channel for
    those markers. Case-insensitive on the tag sequences.
    """
    if not text:
        return text
    if strict is None:
        strict = clamp_active()
    if strict:
        # First, so an invisible character cannot hide a marker or a tag from the passes below.
        text = strict_text(text, clamp_repo())
    for needle, replacement in _DEFANG_SEQUENCES:
        if needle in _CASE_INSENSITIVE_NEEDLES:
            # Case-insensitive replace (e.g. <DETAILS>, <Details>, **[f2]**).
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
            text = pattern.sub(replacement, text)
        else:
            text = text.replace(needle, replacement)
    # #886: the same HTML-comment breakout risk applies to this repo's own
    # metadata markers (id-map/verdicts/usage/etc, marker.py), not just
    # structural HTML. `<!--`/`-->` above already covers the default marker
    # form; `[//]: # (` is the hidden reference-link form (marker.py's
    # `*_HIDDEN_PREFIX` constants), which `extract_verdicts`/`extract_id_map`
    # check on every provider regardless of which one actually wrote the
    # comment. This used to defang only the single literal spelling
    # `"[//]: # ("`, but every hidden-form regex in marker.py tolerates
    # optional spaces/tabs around the `#` (it's a real Markdown link
    # reference definition, and renderers accept that whitespace) -- a
    # variant like `"[//]:#("` survived the literal-string defang and still
    # parsed as a real marker. Matching `HIDDEN_MARKER_OPENER_RE` -- the same
    # pattern every hidden-form extractor builds from -- instead of a
    # separately maintained literal string is what keeps this in sync with
    # whatever those extractors actually accept.
    text = HIDDEN_MARKER_OPENER_RE.sub(
        lambda m: m.group(0)[:-1] + "​" + m.group(0)[-1], text
    )
    return neutralize_references(text)


def suggestion_code(finding: Finding) -> str:
    """The finding's suggested code, or an empty string inside a fork run.

    A fork run drops the code and does not rewrite it. A rewritten suggestion could still be
    applied with one click, and the author of the fork controls what the model saw.
    """
    return "" if clamp_active() else finding.suggested_code


_LINE_BREAK_RE: Final[re.Pattern[str]] = re.compile(
    "[\r\n\v\f\x1c\x1d\x1e\x85  ]"
)


def also_reported_notes(finding: Finding) -> list[str]:
    """Return the sanitized "also reported" notes of a merged finding (#1044).

    `also_reported` is built from analyzer output and can carry untrusted
    file text, so every note goes through `sanitize_bullet_text` here, once,
    for all renderers. The list is capped at `MAX_ALSO_REPORTED` notes here,
    at render time. A last line counts the notes left out. It says "or more"
    when the stored list hit the storage bound.
    """
    real, truncated = split_also_reported(finding.also_reported)
    notes = [sanitize_bullet_text(n) for n in real[:MAX_ALSO_REPORTED]]
    extra = len(real) - MAX_ALSO_REPORTED
    if extra > 0:
        more = "or more" if truncated else "more"
        notes.append(f"(+{extra} {more} from other analyzers)")
    return notes


def sanitize_bullet_text(text: str) -> str:
    """Sanitize `finding`/`remediation` text for a single-line bullet render
    (issue #914).

    Collapses every line-break character `str.splitlines()` recognizes to a
    space, then applies `sanitize_display_text`'s HTML/marker defang. A
    forged embedded newline would otherwise let untrusted text render as an
    independent line -- the bullet-scanners in `vcs/_finding_ids.py` and
    `slash/github_orchestration.py` both iterate `body.splitlines()`, so a
    prompt-injected `\n- **[F2]** ...` (or a fake `###`/`</details>`
    section-boundary line) would be parsed as real structure. `str`'s own
    line-break set is wider than `\r`/`\n` (it also includes `\v`, `\f`, and
    several Unicode separators), so all of them are collapsed, not just the
    two ASCII ones.

    Deliberately a separate function from `sanitize_display_text` rather
    than a parameter on it: that function is also called elsewhere on
    genuinely multi-line trusted text (`agents/summarizer.py`,
    `review/preflight.py`) and must keep working on real newlines there.
    """
    return sanitize_display_text(_LINE_BREAK_RE.sub(" ", text))


def format_source_tag(finding: Finding) -> str:
    """Render `[agent1, agent2]` tag from a finding's sources or source field.

    Sanitized (issue #913): `source`/`sources` can carry untrusted text --
    findings/extract.py now overwrites LLM-agent-supplied values, but this
    is the single render-time choke point for every ingestion path,
    including ones extract.py never sees (e.g. SARIF's `source = f"sarif:
    {driver_name}"`, where `driver_name` comes from uploaded SARIF tool
    metadata). Sanitizing here means a future ingestion path can't
    reintroduce this bug class by skipping extract.py's stamp.
    """
    if finding.sources:
        return f"[{', '.join(sanitize_display_text(s) for s in finding.sources)}]"
    if finding.source:
        return f"[{sanitize_display_text(finding.source)}]"
    return ""


def badge_suffix(finding: Finding) -> str:
    """Italic evidence label placed after a finding's text, or an empty string.

    The label is computed here from the finding's current fields, using fixed
    phrases and a number, so it is not user text. It goes after the severity and
    ``F`` tokens that the comment parsers read, and after the finding text, so it
    cannot change those tokens.
    """
    if not finding.show_badge:
        return ""
    from ai_pr_review.findings.badge import finding_badge

    return f" _({sanitize_bullet_text(finding_badge(finding))})_"


def detail_lines(finding: Finding) -> list[tuple[str, str]]:
    """Return the ``(label, text)`` pairs for a finding's impact and trigger (#1078).

    The text comes from an agent, so it goes through ``sanitize_bullet_text``
    like the finding text. A finding with neither field returns an empty list.
    """
    pairs: list[tuple[str, str]] = []
    if finding.impact:
        pairs.append(("Impact", sanitize_bullet_text(finding.impact)))
    if finding.trigger:
        pairs.append(("Trigger", sanitize_bullet_text(finding.trigger)))
    return pairs


def format_body_finding(
    finding: Finding,
    *,
    location_note: str = "",
    include_suggestion: bool = False,
    include_remediation: bool = True,
    finding_id: int | None = None,
) -> str:
    """Render a finding as a single Markdown bullet for the review body.

    Parameters
    ----------
    finding_id:
        Optional stable per-PR numeric ID (e.g. 1 → ``**[F1]**``).  When
        provided, the ID token is inserted between the severity and source
        tags so users can reference it in ``/ai-pr-review dismiss F1``.
    include_remediation:
        Default True. Pass False to omit the remediation sub-bullet --
        issue #919: Bitbucket renders a shortened bullet (no remediation)
        for a finding whose full detail already lives in a Code Insights
        annotation on the diff, to avoid duplicating that text in the
        comment body while still keeping the finding itself visible there
        (severity/text/location), rather than omitting it entirely.
    """
    icon = severity_icon(finding.severity)
    source_tag = format_source_tag(finding)
    location = ""
    if finding.file:
        # finding.file is agent-derived, not confirmed against the diff's
        # real file set for a body-rendered finding (only the inline-anchor
        # path checks membership). Neutralize it the same way github.py's
        # carried-forward-thread rendering already treats a PR-author-
        # controlled path: strip CR/LF and backticks (sanitize_display_text
        # only defangs structure-breaking HTML, not Markdown, and a raw
        # backtick would terminate the code span early), then run it through
        # sanitize_bullet_text so a forged `<!-- ai-pr-review-verdicts: ... -->`
        # (or any other marker-shaped string, or a bullet-forgery attempt --
        # issue #914) can't survive into the rendered body and be parsed
        # back as real state on a later run (e.g. Bitbucket's post_findings,
        # which reads verdicts straight out of its own previously-rendered
        # comment body). Uses sanitize_bullet_text rather than a hand-rolled
        # \r/\n strip (issue #914 follow-up): str.splitlines() recognizes
        # several other line-break codepoints (\v, \f, U+2028, ...) that a
        # bare .replace("\r", "").replace("\n", " ") left open as a way to
        # inject a standalone forged line via `finding.file`.
        safe_file = finding.file.replace("`", "'")
        loc_parts = [sanitize_bullet_text(safe_file)]
        if finding.line is not None:
            loc_parts.append(str(finding.line))
        location = ":".join(loc_parts)
    header_parts = [icon, f"**[{finding.severity}]**"]
    if finding_id is not None:
        header_parts.append(f"**[F{finding_id}]**")
    if source_tag:
        header_parts.append(source_tag)
    header_parts.append(sanitize_bullet_text(finding.finding) + badge_suffix(finding))
    out = "- " + " ".join(header_parts)
    if location:
        out += f" *(at `{location}`{location_note})*"
    # Impact and trigger are detail sub-bullets, so the shortened Bitbucket bullet
    # (include_remediation=False) leaves them out too (#1078).
    if include_remediation:
        for label, text in detail_lines(finding):
            out += f"\n  - **{label}:** {text}"
    if include_remediation and finding.remediation:
        out += f"\n  - **Remediation:** {sanitize_bullet_text(finding.remediation)}"
    # The shortened Bitbucket bullet (include_remediation=False) leaves out detail
    # sub-bullets, and its Code Insights annotation already carries the notes (#919).
    if include_remediation:
        for note in also_reported_notes(finding):
            out += f"\n  - **Also reported by:** {note}"
    code = suggestion_code(finding)
    if include_suggestion and code:
        fence_body = code.replace("```", "``​`")
        out += f"\n  ```\n  {fence_body}\n  ```"
    return out


# Opening lines of the token-usage accordion built by
# `review.reporting.build_token_table_accordion`. Anchoring on this exact
# two-line prefix (rather than the bare `<details>` tag) lets a provider
# find *its own* token table unambiguously even when other `<details>`
# blocks (e.g. a collapsed Walkthrough section) appear earlier in the same
# body -- see the GitLab summary-note upsert in vcs/gitlab.py, which used to
# truncate at the first `<details>` anywhere in the body (issue found while
# adding the Walkthrough accordion).
TOKEN_TABLE_OPEN_MARKER: Final[str] = "<details>\n<summary>Token usage by agent</summary>"


def truncate_body(body: str, limit: int = GITHUB_MAX_BODY_SIZE) -> str:
    """Truncate body at byte boundary, append marker. UTF-8 safe.

    Cuts at `limit` bytes, then drops trailing partial UTF-8 by decoding with
    errors='ignore'.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= limit:
        return body
    head = encoded[:limit].decode("utf-8", errors="ignore")
    trailer = (
        "\n\n---\n"
        f"*{TRUNCATION_MARKER} — body exceeded provider API limit "
        f"({limit:,} bytes). Run a full review locally to see complete output.*"
    )
    return head + trailer


def _prompt_text(text: str) -> str:
    """Make one string safe inside the agent-prompt JSON block.

    Run it through ``sanitize_display_text``, and break a triple backtick that would close the fence
    around the JSON (``json.dumps`` does not escape backticks).
    """
    return sanitize_display_text(text).replace("```", "``\u200b`")


def build_agent_prompt(findings: Sequence[Finding]) -> str:
    """Render the collapsible "Prompt for AI agents" block from findings.

    Placed at the end of the review body so users can copy-paste into an AI
    tool to remediate. Every string goes through ``_prompt_text`` (story 17-11):
    the finding text is model output that the author of the pull request can steer.
    """
    if not findings:
        return ""
    items = []
    for f in findings:
        location = ""
        if f.file:
            location = f.file
            if f.line is not None:
                location = f"{f.file}:{f.line}"
        entry: dict[str, object] = {
            "severity": f.severity,
            "finding": _prompt_text(f.finding),
        }
        if location:
            entry["location"] = _prompt_text(location)
        if f.impact:
            entry["impact"] = _prompt_text(f.impact)
        if f.trigger:
            entry["trigger"] = _prompt_text(f.trigger)
        if f.remediation:
            entry["remediation"] = _prompt_text(f.remediation)
        items.append(entry)
    payload = json.dumps(items, indent=2)
    return (
        "<details>\n"
        "<summary>🤖 Prompt for AI agents</summary>\n\n"
        "Copy the JSON below into an AI coding assistant to triage:\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n"
        "</details>"
    )


def join_findings(items: Iterable[str]) -> str:
    """Join body-finding bullets with a blank line between them."""
    return "\n".join(items)


def render_skip_findings_section(findings: Sequence[Finding]) -> str:
    """Render analyzer/SARIF findings computed before a cost-ceiling skip
    (#896) as a body-level section for ``post_skip_comment``.

    Returns ``""`` when ``findings`` is empty, so a caller can unconditionally
    append the result without an ``if findings:`` guard. No ``finding_id`` is
    assigned (this bypasses ``post_findings``'s id-map/canonical-review
    machinery entirely — these findings have no F-ID, and no slash command
    can target them individually).
    """
    if not findings:
        return ""
    bullets = [format_body_finding(f) for f in findings]
    return "\n\n### Static analysis findings (LLM review skipped)\n" + join_findings(bullets)
