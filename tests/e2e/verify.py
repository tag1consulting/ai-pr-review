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


def verify_event_not_degraded(posted_event: str, expected_event: str,
                               telemetry: dict[str, Any] | None = None,
                               summary_body: str = "") -> Verdict:
    """Fail if a review requested as APPROVE/REQUEST_CHANGES was actually
    posted as a plain COMMENT (the self-approval degrade case, issue #651).

    *posted_event* (parsed from the container's "Review complete: ...
    event=..." stderr line, see parse_review_log_line) is the primary and
    only authoritative signal: verified against cli.py/orchestrate.py that
    it reflects the event actually posted, including a post-time degrade.
    *telemetry* is NOT used for this check even when present: its `outcome`
    field is assembled from the pre-posting classification decision
    (orchestrate.py's classify_review_outcome, threaded through as
    `outcome.event` to the posting call) and is never updated to reflect a
    degrade that happens during posting -- an earlier version of this
    function preferred telemetry.outcome first, which meant it could never
    actually catch the #651 degrade it exists to check for (telemetry
    would always report the original APPROVE intent, not the COMMENT that
    was really posted). summary_body's degrade-fallback text
    ("has NOT been approved") is kept as a secondary corroborating check
    only, for the case where posted_event is unavailable.
    """
    if expected_event not in ("APPROVE", "REQUEST_CHANGES"):
        return Verdict("event_not_degraded", True, f"expected_event={expected_event!r}; degrade check not applicable")

    if posted_event:
        if posted_event != expected_event:
            return Verdict(
                "event_not_degraded", False,
                f"posted_event={posted_event!r} != expected_event={expected_event!r} "
                "(source: posted_event, the actually-posted event)",
            )
        return Verdict("event_not_degraded", True, f"posted_event={posted_event!r} matches expected_event")

    # posted_event unavailable (e.g. the log line failed to parse) -- fall
    # back to the summary body's degrade-fallback text as a weaker signal.
    if _DEGRADE_TEXT in summary_body:
        return Verdict(
            "event_not_degraded", False,
            f"summary body contains degrade-fallback text {_DEGRADE_TEXT!r} "
            "(source: summary_body text scan; posted_event unavailable)",
        )
    return Verdict(
        "event_not_degraded", False,
        "posted_event unavailable and no degrade-fallback text found in summary_body "
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
        Verdict("posting_inline", bool(evidence.inline_comments),
                f"{len(evidence.inline_comments)} inline comment(s)" if evidence.inline_comments
                else "no inline comments found",
                pagination_note=note),
    ]
    if bitbucket:
        verdicts.append(
            Verdict("posting_annotations", bool(evidence.annotations),
                    f"{len(evidence.annotations)} annotation(s)" if evidence.annotations
                    else "no annotations found",
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
        return any(exp.path_substring in h and exp.category in h for h in haystacks)

    missing = [exp for exp in expected if not _found(exp)]
    approx_count = len(evidence.inline_comments) if evidence.inline_comments else (
        1 if body.strip() else 0
    )
    if approx_count < findings_floor:
        return Verdict(
            "analyzer_findings", False,
            f"approx findings count {approx_count} below floor {findings_floor}",
        )
    if missing:
        return Verdict(
            "analyzer_findings", False,
            f"{len(missing)}/{len(expected)} expected finding(s) not found: "
            f"{[(m.path_substring, m.category) for m in missing]}",
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
