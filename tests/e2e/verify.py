"""Pure verification functions for the e2e harness.

Every function in this module (except `load_telemetry`, the one documented
exception) takes already-fetched data structures and does no I/O -- this
keeps them cleanly unit-testable against synthetic fixtures without needing
a live network or a real container run (see
tests/python/e2e_harness/test_verify.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_pr_review.vcs.marker import extract_summary_sha

from .config import ExpectedFinding
from .platforms import RawEvidence


class HarnessError(Exception):
    """Base class for all harness-raised errors."""


class InfraFailure(HarnessError):
    """The harness itself, its environment, or the container invocation
    failed -- not a product defect. E.g. missing/malformed telemetry, auth
    failure, cost-ceiling skip, timeout."""


class ProductFailure(HarnessError):
    """The review ran to completion but produced incorrect behavior -- e.g.
    a missing summary marker, a degraded APPROVE->COMMENT, a wrong model."""


_REVIEW_COMPLETE_RE = re.compile(
    r"Review complete:\s*(?P<findings>\d+)\s*findings,\s*(?P<failed>\d+)\s*failed agents"
    r"(?:,\s*event=(?P<event>[^,\s]+))?"
    r"(?:,\s*base=(?P<base>[^,\s]+))?",
)
_REVIEW_SKIPPED_RE = re.compile(r"Review skipped:\s*(?P<reason>.+)")


@dataclass(frozen=True)
class ReviewLogResult:
    skipped: bool
    findings_count: int | None = None
    failed_agents: int | None = None
    event: str | None = None
    base: str | None = None
    skip_reason: str | None = None


def parse_review_log_line(stderr_text: str) -> ReviewLogResult:
    """Parse the container's stderr for its terminal "Review complete: ..."
    or "Review skipped: ..." line. Raises InfraFailure if neither is found
    -- a container that produced no such line did not complete a review run
    in any recognizable state, which is an infra problem, not a product one.
    """
    skip_match = _REVIEW_SKIPPED_RE.search(stderr_text)
    if skip_match:
        return ReviewLogResult(skipped=True, skip_reason=skip_match.group("reason").strip())

    complete_match = _REVIEW_COMPLETE_RE.search(stderr_text)
    if complete_match:
        return ReviewLogResult(
            skipped=False,
            findings_count=int(complete_match.group("findings")),
            failed_agents=int(complete_match.group("failed")),
            event=complete_match.group("event"),
            base=complete_match.group("base"),
        )

    raise InfraFailure(
        "container stderr contained neither a 'Review complete: ...' nor a "
        "'Review skipped: ...' line; the review did not reach a recognizable "
        "terminal state"
    )


def load_telemetry(path: Path) -> dict[str, Any]:
    """Load and validate the telemetry JSON file at *path*.

    Raises InfraFailure (not silently returning {}) if the file is missing,
    empty, malformed JSON, or missing required fields -- a broken telemetry
    sink is itself an infra failure the harness must surface, never a signal
    to fall back to weaker evidence unnoticed.
    """
    if not path.exists():
        raise InfraFailure(f"telemetry file not found at {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise InfraFailure(f"telemetry file at {path} is empty")
    # file:// sink is JSONL (one object per line, appended); a run should
    # produce exactly one line, but be lenient and take the last one in case
    # of a leftover/reused path.
    last_line = text.splitlines()[-1]
    try:
        data: dict[str, Any] = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise InfraFailure(f"telemetry file at {path} contains malformed JSON: {exc}") from exc

    required_fields = (
        "outcome", "findings_count", "findings_by_severity", "failed_agents",
        "token_usage_by_agent", "provider", "model_standard", "model_premium",
        "review_mode",
    )
    missing = [f for f in required_fields if f not in data]
    if missing:
        raise InfraFailure(
            f"telemetry file at {path} is missing required field(s): {missing}"
        )
    return data


@dataclass(frozen=True)
class Verdict:
    name: str
    ok: bool
    reason: str
    category: str = "product_failure"  # or "infra_failure"
    pagination_note: str | None = None


_DEGRADE_TEXT = "has NOT been approved"


def verify_summary_marker(evidence: RawEvidence, expected_sha: str) -> Verdict:
    """Check the summary marker (either GitHub/GitLab's visible HTML-comment
    form or Bitbucket's hidden link-reference form), and that the SHA
    matches this run's own commit -- not a stale marker left by a prior run
    against the same PR/MR.

    Uses ai_pr_review.vcs.marker.extract_summary_sha (the product's own
    marker parser) rather than a hand-rolled regex duplicated here: an
    earlier version of this function used an invented marker format
    (`<!-- ai-pr-review:sha:... -->`) that the product has never emitted
    (the real format is `<!-- ai-pr-review-summary sha=... -->` /
    `[//]: # (ai-pr-review-summary sha=...)`), which would have made every
    live run fail this check regardless of whether posting actually worked.
    This is a deliberate, narrow exception to platforms.py's "no
    ai_pr_review.vcs dependency" rule: that rule is about not sharing
    *posting* logic with the layer that independently re-fetches evidence;
    reusing the read-only marker *parser* here means the verifier tracks
    the real format instead of drifting from it, which is a correctness
    improvement, not the coupling that rule guards against.
    """
    sha = extract_summary_sha(evidence.summary_body)
    if not sha:
        return Verdict("summary_marker", False, "no sha marker (visible or hidden) found in summary body")
    if not expected_sha.startswith(sha) and not sha.startswith(expected_sha):
        return Verdict(
            "summary_marker", False,
            f"marker sha {sha!r} does not match this run's commit {expected_sha!r} "
            "(stale marker from a prior run?)",
        )
    return Verdict("summary_marker", True, f"marker sha {sha!r} matches run commit")


def verify_event_not_degraded(posted_event: str, telemetry: dict[str, Any] | None = None,
                               summary_body: str = "") -> Verdict:
    """Fail if the review's own intended decision (APPROVE or
    REQUEST_CHANGES) was actually posted as a plain COMMENT (the
    self-approval degrade case, issue #651).

    Compares two signals against each other, not against a hardcoded
    expectation:

    - *posted_event* (parsed from the container's "Review complete: ...
      event=..." stderr line, see parse_review_log_line): verified against
      cli.py/orchestrate.py to reflect the event actually posted, including
      a post-time degrade.
    - telemetry's `outcome` field: the pre-posting classification decision
      (orchestrate.py's classify_review_outcome, threaded through as
      `outcome.event` to the posting call), never updated to reflect a
      degrade that happens during posting -- this is what the review
      *intended* to post, not necessarily what it did.

    An earlier version of this function took a hardcoded `expected_event`
    string (always "APPROVE") instead of reading intent from telemetry.
    That was wrong in two different ways, both caught live: it happened to
    catch a real GitHub degrade (intent was actually REQUEST_CHANGES,
    posted COMMENT -- coincidentally also != "APPROVE") but then produced a
    false failure on GitLab, where the review correctly intended AND
    posted REQUEST_CHANGES (a legitimate outcome for a fixture with real
    findings, never actually degraded) -- flagged as a failure only
    because REQUEST_CHANGES != the hardcoded "APPROVE". The fix is to
    compare posted_event against telemetry's own stated intent, not an
    assumption that every review should end in approval.

    summary_body's degrade-fallback text ("has NOT been approved") is kept
    as a secondary corroborating check only, for when telemetry is
    unavailable.
    """
    if telemetry is not None:
        if "outcome" not in telemetry:
            # load_telemetry() requires this field on the real call path, so
            # a telemetry dict missing it entirely means malformed/corrupt
            # telemetry (schema drift, partial write), not a legitimate
            # non-approve/request-changes outcome -- keep that distinct from
            # the "not applicable" case below instead of defaulting to "".
            return Verdict("event_not_degraded", False,
                            "telemetry present but missing 'outcome' field -- malformed telemetry, "
                            "cannot verify degrade")
        intended = telemetry["outcome"]
        if intended not in ("APPROVE", "REQUEST_CHANGES"):
            return Verdict("event_not_degraded", True,
                            f"intended outcome={intended!r}; degrade check not applicable")
        if not posted_event:
            # posted_event is unavailable (e.g. the "Review complete: ..."
            # log line lacked an `event=` group) -- fall through to the
            # summary_body text scan below rather than treating an empty
            # string as a vacuous match against `intended`.
            return _check_degrade_text_fallback(summary_body, reason_suffix="posted_event unavailable")
        if posted_event != intended:
            return Verdict(
                "event_not_degraded", False,
                f"intended outcome={intended!r} but posted_event={posted_event!r} "
                "(source: telemetry.outcome vs. posted_event)",
            )
        return Verdict("event_not_degraded", True,
                        f"posted_event={posted_event!r} matches intended outcome={intended!r}")

    # telemetry unavailable -- fall back to the summary body's
    # degrade-fallback text as a weaker signal.
    return _check_degrade_text_fallback(summary_body, reason_suffix="telemetry unavailable")


def _check_degrade_text_fallback(summary_body: str, *, reason_suffix: str) -> Verdict:
    """Shared fallback for verify_event_not_degraded when posted_event and/or
    telemetry can't confirm the outcome directly: scan summary_body for the
    degrade-fallback text as a weaker corroborating signal.
    """
    if _DEGRADE_TEXT in summary_body:
        return Verdict(
            "event_not_degraded", False,
            f"summary body contains degrade-fallback text {_DEGRADE_TEXT!r} "
            f"({reason_suffix})",
        )
    return Verdict(
        "event_not_degraded", False,
        f"no degrade-fallback text found in summary_body ({reason_suffix}) "
        "-- cannot confirm the review was not degraded",
    )


def verify_no_failed_agents(log_result: ReviewLogResult, telemetry: dict[str, Any] | None) -> Verdict:
    """Fail if any review agent failed mid-run, even though the overall
    review completed (a degraded/partial review, not a crash).

    The container's own log line and telemetry both carry this count/list,
    but neither was ever checked anywhere before this function existed --
    the harness collected exactly the signal needed to catch a partially-
    failed review and then dropped it on the floor, letting a degraded run
    pass as a clean release-gate result.
    """
    telemetry_failed = telemetry.get("failed_agents") if telemetry else None
    if isinstance(telemetry_failed, list) and telemetry_failed:
        return Verdict(
            "no_failed_agents", False,
            f"telemetry reports {len(telemetry_failed)} failed agent(s): {telemetry_failed}",
        )
    if log_result.failed_agents:
        return Verdict(
            "no_failed_agents", False,
            f"log line reports {log_result.failed_agents} failed agent(s)",
        )
    return Verdict("no_failed_agents", True, "no failed agents reported")


def verify_model(telemetry: dict[str, Any], expected_model_id: str) -> Verdict:
    """Check by model ID (telemetry's model_standard/model_premium), not a
    hardcoded display string."""
    standard = telemetry.get("model_standard", "")
    premium = telemetry.get("model_premium", "")
    if expected_model_id in (standard, premium):
        return Verdict("model", True, f"expected model {expected_model_id!r} found "
                                       f"(standard={standard!r}, premium={premium!r})")
    return Verdict(
        "model", False,
        f"expected model {expected_model_id!r} not found in telemetry "
        f"(standard={standard!r}, premium={premium!r})",
    )


def verify_posting_surfaces(evidence: RawEvidence, *, bitbucket: bool = False) -> list[Verdict]:
    """Independently verify summary, inline/discussions, and (Bitbucket
    only) annotations were posted at all. Each Verdict carries a
    pagination_note when evidence.truncated is True, so a truncated fetch
    warns rather than silently passing/failing on incomplete data.
    """
    note = (
        "evidence fetch hit the pagination safety bound (20 pages); "
        "result may be incomplete"
        if evidence.truncated else None
    )
    verdicts = [
        Verdict("posting_summary", bool(evidence.summary_body.strip()),
                "summary body present" if evidence.summary_body.strip() else "summary body empty",
                pagination_note=note),
    ]
    if bitbucket:
        # Bitbucket has no inline-review support (per the harness design and
        # ai_pr_review/vcs/bitbucket.py:post_findings) -- per-finding
        # comments never exist there, so posting_inline would always and
        # meaninglessly fail. Code Insights annotations are Bitbucket's
        # equivalent per-finding surface; verify that instead.
        verdicts.append(
            Verdict("posting_annotations", bool(evidence.annotations),
                    f"{len(evidence.annotations)} annotation(s)" if evidence.annotations
                    else "no annotations found",
                    pagination_note=note)
        )
    else:
        verdicts.append(
            Verdict("posting_inline", bool(evidence.inline_comments),
                    f"{len(evidence.inline_comments)} inline comment(s)" if evidence.inline_comments
                    else "no inline comments found",
                    pagination_note=note)
        )
    return verdicts


def verify_analyzer_findings(evidence: RawEvidence, expected: list[ExpectedFinding], *, findings_floor: int) -> Verdict:
    """Check the fixture-specific deterministic assertions plus a low
    findings-count floor as a sanity backstop (NOT the old harness's >=10
    threshold, which was tuned against unreliable LLM-transcribed output).
    """
    body = evidence.summary_body
    inline_bodies = [c.get("body", "") or c.get("content", {}).get("raw", "") for c in evidence.inline_comments]
    haystacks = [body, *inline_bodies]

    def _found(exp: ExpectedFinding) -> bool:
        if any(exp.path_substring in h and exp.category in h for h in haystacks):
            return True
        # Bitbucket has no inline comments (see verify_posting_surfaces) --
        # its per-finding text lives in each Code Insights annotation's
        # `summary` field, with the file path in a separate `path` field.
        # Check those two fields independently per-annotation rather than
        # concatenating them into one string: an earlier version joined
        # `path` and `summary` before matching, which let an annotation on
        # a DIFFERENT file satisfy path_substring merely by mentioning that
        # path in its own summary text (e.g. "same pattern as api/user.py").
        return any(
            exp.path_substring in a.get("path", "") and exp.category in a.get("summary", "")
            for a in evidence.annotations
        )

    missing = [exp for exp in expected if not _found(exp)]
    per_finding_count = len(evidence.inline_comments) + len(evidence.annotations)
    approx_count = per_finding_count if per_finding_count else (1 if body.strip() else 0)
    # A truncated fetch (fetch_annotations/fetch_inline hit their pagination
    # safety bound) means the counts and matches above are known-incomplete
    # -- surface that on any failure the same way verify_posting_surfaces
    # already does, so a false "below floor"/"not found" doesn't read as a
    # genuine review regression.
    truncation_note = (
        " (evidence fetch hit the pagination safety bound; result may be incomplete)"
        if evidence.truncated else ""
    )
    if approx_count < findings_floor:
        return Verdict(
            "analyzer_findings", False,
            f"approx findings count {approx_count} below floor {findings_floor}{truncation_note}",
        )
    if missing:
        return Verdict(
            "analyzer_findings", False,
            f"{len(missing)}/{len(expected)} expected finding(s) not found: "
            f"{[(m.path_substring, m.category) for m in missing]}{truncation_note}",
        )
    return Verdict("analyzer_findings", True, f"approx findings count {approx_count}, all expected findings present")


@dataclass(frozen=True)
class RunResult:
    platform: str
    category: str  # "pass" | "product_failure" | "infra_failure"
    verdicts: tuple[Verdict, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.category == "pass"


def aggregate(platform: str, verdicts: list[Verdict], *, infra_error: HarnessError | None = None) -> RunResult:
    """Combine verdicts (and an optional already-raised HarnessError) into
    one RunResult with a category and machine-readable reasons."""
    if infra_error is not None:
        return RunResult(
            platform=platform, category="infra_failure",
            verdicts=tuple(verdicts), reasons=(str(infra_error),),
        )
    failed = [v for v in verdicts if not v.ok]
    if not failed:
        return RunResult(platform=platform, category="pass", verdicts=tuple(verdicts))
    return RunResult(
        platform=platform, category="product_failure",
        verdicts=tuple(verdicts),
        reasons=tuple(f"{v.name}: {v.reason}" for v in failed),
    )
