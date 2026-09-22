"""Bitbucket Code Insights: the display layer for Bitbucket findings.

Per the Bitbucket-parity plan (issues #839/#873/#874), findings render
inline on the PR diff via Code Insights annotations instead of Bitbucket
comment threads -- there is no thread state to build or resolve here, only
a report and its annotations, rebuilt from scratch every run. This module
owns exactly that: the DELETE -> PUT -> POST report/annotation lifecycle
and the category -> annotation_type mapping. The single summary comment
(bitbucket.py) remains the state/verdict layer; this module never posts a
comment or reads/writes a verdicts marker itself.

The report PUT is an upsert, but annotations under it accumulate -- a bare
re-PUT does not clear the previous run's annotations. DELETE-then-PUT-then-
POST every run is what makes a fixed/suppressed finding drop out of the
next run's report with no extra bookkeeping: nothing to un-render, nothing
to track across runs beyond the report's own contents.

Isolation constraint (Bitbucket-parity plan, Phase 3): this module and its
caller in bitbucket.py only ever *call into*
``ai_pr_review.vcs._canonical.classify()`` -- they never modify that
module's logic. A change to shared classification code is a separately
reviewed change gated on the GitHub/GitLab test suites staying green, not
something folded into Bitbucket-only work.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ai_pr_review.findings.models import CATEGORIES, Finding, Severity
from ai_pr_review.vcs._finding_ids import fingerprint
from ai_pr_review.vcs.http import RecordingClient

REPORT_ID: Final[str] = "ai-pr-review"
MAX_ANNOTATIONS_PER_BATCH: Final[int] = 100
_MAX_TITLE_BYTES: Final[int] = 450
_MAX_SUMMARY_BYTES: Final[int] = 2_000

# Category -> Code Insights annotation_type. VULNERABILITY = an attacker can
# exploit it; BUG = wrong at runtime (test-gap included: a missing test on a
# security-relevant path is not cosmetic, and Bitbucket renders BUG more
# prominently than CODE_SMELL); CODE_SMELL = maintainability. Kept as one
# dict with an exhaustiveness test (test_code_insights.py) asserting
# set(_ANNOTATION_TYPE) == set(CATEGORIES), so a 12th category added to the
# shared taxonomy is a test failure here, not a silent fall-through.
_ANNOTATION_TYPE: Final[dict[str, str]] = {
    "authz": "VULNERABILITY",
    "injection": "VULNERABILITY",
    "secret": "VULNERABILITY",
    "dependency-cve": "VULNERABILITY",
    "edge-case": "BUG",
    "test-gap": "BUG",
    "architecture-coupling": "CODE_SMELL",
    "observability": "CODE_SMELL",
    "docs": "CODE_SMELL",
    "lint": "CODE_SMELL",
    "other": "CODE_SMELL",
}
assert set(_ANNOTATION_TYPE) == set(CATEGORIES), (
    "ai_pr_review.vcs._code_insights._ANNOTATION_TYPE has drifted from "
    "ai_pr_review.findings.models.CATEGORIES -- add the new category here"
)

_ANNOTATION_SEVERITY: Final[dict[Severity, str]] = {
    "Critical": "CRITICAL",
    "High": "HIGH",
    "Medium": "MEDIUM",
    "Low": "LOW",
}


@dataclass(frozen=True)
class CodeInsightsResult:
    """Outcome of posting one run's Code Insights report + annotations.

    ``posted_findings`` (not just a count) lets a caller recover from a
    partial batch failure precisely: on error, everything up through the
    last fully-succeeded batch is already live on Bitbucket and must not
    also be re-rendered in the summary comment body, or it would show
    twice. Only the tail this call never got to should fall back.
    """

    posted_findings: tuple[Finding, ...] = ()
    error: str | None = None

    @property
    def posted(self) -> int:
        return len(self.posted_findings)

    @property
    def ok(self) -> bool:
        return self.error is None


def report_url(workspace: str, repo_slug: str, commit: str) -> str:
    return f"/repositories/{workspace}/{repo_slug}/commit/{commit}/reports/{REPORT_ID}"


def annotations_url(workspace: str, repo_slug: str, commit: str) -> str:
    return f"{report_url(workspace, repo_slug, commit)}/annotations"


def build_report_payload(all_active_findings: Sequence[Finding]) -> dict[str, Any]:
    """The report itself carries only a title/summary/pass-fail.

    ``all_active_findings`` must be every still-active (non-suppressed)
    finding this run, not just the subset that gets an annotation -- a
    Critical/High finding that is out-of-diff or bumped to the body by
    ``max_inline`` still has to make the report say FAILED. Using only the
    annotated subset here would let Bitbucket's Reports tab say PASSED in
    the same run where the summary comment shows a blocking finding and
    ``event=REQUEST_CHANGES``, which is the one signal Code Insights exists
    to surface accurately.
    """
    has_blocking = any(f.severity in ("Critical", "High") for f in all_active_findings)
    return {
        "title": "AI PR Review",
        "details": (
            f"{len(all_active_findings)} finding(s) from ai-pr-review."
            if all_active_findings
            else "No findings from ai-pr-review."
        ),
        "report_type": "BUG",
        "result": "FAILED" if has_blocking else "PASSED",
        "reporter": "ai-pr-review",
    }


def build_annotation_payload(
    finding: Finding, *, finding_id: int | None
) -> dict[str, Any] | None:
    """Return one finding's annotation payload, or None if it has no
    file/line to anchor to -- an annotation, like an inline PR comment,
    requires a concrete location. Callers are expected to only pass
    findings that already passed inline-eligibility (partition_findings),
    so this should never actually return None in practice; the guard exists
    so a caller that skips that precondition fails safe instead of posting
    a malformed payload.

    ``summary`` carries the ``[F<n>]`` token so a human reading the
    annotation on Bitbucket's diff view knows what to reference in a
    verdict command against the summary comment (Phase 4). Kept legible
    without markdown -- Atlassian's own Code Insights docs don't confirm
    whether ``summary``/``details`` render markdown, so this assumes
    plain-text only.
    """
    if not finding.file or finding.line is None:
        return None
    token = f"[F{finding_id}] " if finding_id is not None else ""
    summary = f"{token}{finding.severity}: {finding.finding}"
    if finding.remediation:
        summary = f"{summary}\n\nRemediation: {finding.remediation}"
    return {
        "external_id": fingerprint(finding),
        "title": _truncate_utf8(summary, _MAX_TITLE_BYTES),
        "annotation_type": _ANNOTATION_TYPE[finding.category],
        "severity": _ANNOTATION_SEVERITY[finding.severity],
        "path": finding.file,
        "line": finding.line,
        "summary": _truncate_utf8(summary, _MAX_SUMMARY_BYTES),
    }


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[: max(0, limit - 1)].decode("utf-8", errors="ignore") + "…"


def post_code_insights(
    client: RecordingClient,
    *,
    workspace: str,
    repo_slug: str,
    commit: str,
    findings: Sequence[Finding],
    all_active_findings: Sequence[Finding],
    id_map: dict[str, int],
) -> CodeInsightsResult:
    """DELETE the prior report (if any), PUT a fresh one, then bulk-POST its
    annotations, in chunks of at most ``MAX_ANNOTATIONS_PER_BATCH``.

    ``findings`` is the inline-eligible subset to annotate.
    ``all_active_findings`` is every still-active finding this run (used
    only for the report's own PASSED/FAILED status and count, see
    ``build_report_payload``).

    Always runs this full cycle, even when ``findings`` is empty -- that is
    exactly how a finding that was fixed or suppressed since the last run
    drops out of the report with no separate tracking.

    Returns ``CodeInsightsResult(error=...)`` on the first 403/404 the
    DELETE or PUT hits (Code Insights not enabled/visible on this
    workspace's plan). A 404 on the DELETE itself is not an error -- it
    means there was nothing to delete yet (first run, or the previous
    report already expired/was removed). A failure partway through the
    annotation POST batches still reports every finding from the
    fully-succeeded batches in ``posted_findings`` -- those are already
    live on Bitbucket, and a caller that re-renders every ``findings``
    entry into the comment body on any error would duplicate them. Callers
    must treat every finding NOT in ``posted_findings`` as "render this one
    in the summary comment body instead", never as silent data loss.
    """
    r_url = report_url(workspace, repo_slug, commit)
    del_resp = client.request("DELETE", r_url)
    if del_resp.status_code != 404 and del_resp.status_code >= 400:
        return CodeInsightsResult(
            error=(
                f"code insights DELETE report: HTTP {del_resp.status_code}: "
                f"{del_resp.text[:200]}"
            ),
        )

    put_resp = client.request(
        "PUT", r_url, json_body=build_report_payload(all_active_findings)
    )
    if put_resp.status_code >= 400:
        return CodeInsightsResult(
            error=(
                f"code insights PUT report: HTTP {put_resp.status_code}: "
                f"{put_resp.text[:200]}"
            ),
        )

    # findings and their built payloads stay index-aligned: every entry in
    # `findings` reaching this call already passed inline eligibility
    # (partition_findings requires file+line), so build_annotation_payload
    # never actually returns None here -- but the pairing is kept explicit
    # rather than assumed, so posted_findings can never drift out of sync
    # with which payloads were actually sent.
    pairs = [
        (f, build_annotation_payload(f, finding_id=id_map.get(fingerprint(f))))
        for f in findings
    ]
    pairs = [(f, payload) for f, payload in pairs if payload is not None]

    a_url = annotations_url(workspace, repo_slug, commit)
    posted: list[Finding] = []
    for i in range(0, len(pairs), MAX_ANNOTATIONS_PER_BATCH):
        chunk = pairs[i : i + MAX_ANNOTATIONS_PER_BATCH]
        resp = client.request(
            "POST", a_url, json_body=[payload for _f, payload in chunk]
        )
        if resp.status_code >= 400:
            return CodeInsightsResult(
                posted_findings=tuple(posted),
                error=(
                    f"code insights POST annotations: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                ),
            )
        posted.extend(f for f, _payload in chunk)
    return CodeInsightsResult(posted_findings=tuple(posted))
