"""LLM candidate-finding judge pass — Story 7-3 (#360 remainder).

After the findings pipeline has merged, suppressed, and scoped findings,
this module sends a single compact LLM call that asks a cheap model to
evaluate each finding's quality and return a ``keep`` or ``downrank``
verdict per finding id.

Design constraints (from user decisions, session 2026-06-22):
- ``drop`` is NOT a valid verdict: the judge never removes a finding.
  A false positive is better than a missed vulnerability.
- ``downrank`` lowers confidence and routes the finding to the review body
  (sets ``demoted_to_body=True``) so it is still visible but does not appear
  as an inline PR comment. Severity is deliberately left unchanged — downrank
  affects placement, not risk. (Prior to the #622 fix this set
  ``out_of_diff=True`` instead, which collided with apply_diff_scope's
  distinct out_of_diff+Low-severity-cap invariant and caused renderers that
  filter on out_of_diff to silently drop a downranked High from the review's
  headline risk/count. See findings/models.py's field docs.)
- Corroborated findings (``finding.corroborated is True``) are ALWAYS kept
  regardless of the judge's verdict. Independent static-analyzer + LLM-agent
  corroboration cannot be overridden by a single judge call.
- The judge is fail-soft: any LLM error, parse error, timeout, or empty
  input returns ``kept`` unchanged and logs a WARNING. A failed judge must
  never modify any finding.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ai_pr_review.agents.dispatch import LLMCall
from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest

logger = logging.getLogger(__name__)

JUDGE_DOWNRANK_AMOUNT: int = 15

JudgeVerdict = Literal["keep", "downrank"]


@dataclass(frozen=True)
class JudgeResult:
    """Result of a judge pass, including modified findings and token usage."""

    findings: list[Finding]
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


def _build_candidate_payload(kept: list[Finding]) -> str:
    """Serialize findings as a compact JSON array for the judge prompt.

    The ``id`` field is the list index in ``kept``. ``_apply_verdicts`` maps
    verdicts back using the same index via ``enumerate``. The two functions
    must always receive the same list in the same order; no sort or filter
    may occur between them.
    """
    items = []
    for idx, f in enumerate(kept):
        items.append({
            "id": idx,
            "severity": f.severity,
            "confidence": f.confidence,
            "sources": f.sources,
            "corroborated": f.corroborated,
            "file": f.file,
            "line": f.line,
            "finding": f.finding,
            "remediation": f.remediation,
        })
    return json.dumps(items, ensure_ascii=False)


def _apply_verdicts(
    kept: list[Finding],
    verdicts: list[dict[str, object]],
) -> tuple[list[Finding], int]:
    """Apply judge verdicts deterministically. Returns (modified list, downrank count).

    Rules:
    - ``corroborated is True`` → always ``keep``, log DEBUG.
    - ``downrank`` → lower confidence by JUDGE_DOWNRANK_AMOUNT (floor 0),
      set demoted_to_body=True so the finding routes to the review body.
      Severity is intentionally untouched.
    - ``keep`` → unchanged.
    - Missing verdict id defaults to ``keep``.

    Note: the judge pass runs on ``kept`` *after* ``apply_diff_scope`` (see
    orchestrate.py's pipeline order), so a finding can legitimately reach this
    function already carrying ``out_of_diff=True, severity="Low"``. This
    function does not skip such findings, so a downrank verdict on one sets
    ``demoted_to_body=True`` on top, giving a Finding with BOTH flags True.
    This is intentional, not an oversight: the two flags are independent
    axes (out_of_diff = analyzer-scope exclusion; demoted_to_body =
    judge-placement decision) and compute_headline() (vcs/_body.py) already
    excludes any out_of_diff finding from the headline regardless of
    demoted_to_body, so the combination degrades safely — it can only ever
    affect a finding already capped to Low, never mask a High/Critical. See
    test_judge.py's combined-flags regression test.
    """
    id_to_verdict: dict[int, str] = {}
    for v in verdicts:
        try:
            vid = int(v["id"])  # type: ignore[call-overload]
            if not (0 <= vid < len(kept)):
                logger.warning("judge: verdict id %d out of range [0, %d); skipping", vid, len(kept))
                continue
            verdict_str = str(v.get("verdict", "keep"))
            id_to_verdict[vid] = verdict_str
        except (KeyError, TypeError, ValueError):
            continue

    result: list[Finding] = []
    downrank_count = 0

    for idx, finding in enumerate(kept):
        if finding.corroborated:
            logger.debug(
                "judge: corroborated finding %d kept regardless of verdict (file=%s line=%s)",
                idx, finding.file, finding.line,
            )
            result.append(finding)
            continue

        verdict = id_to_verdict.get(idx, "keep")
        if verdict == "downrank":
            new_confidence = max(0, finding.confidence - JUDGE_DOWNRANK_AMOUNT)
            # model_copy skips validator re-runs; safe here — only confidence
            # and demoted_to_body are updated and neither has cross-field
            # validation. severity is deliberately untouched: downrank means
            # "less prominent placement," not "lower risk."
            result.append(finding.model_copy(update={"confidence": new_confidence, "demoted_to_body": True}))
            downrank_count += 1
        else:
            result.append(finding)

    return result, downrank_count


async def judge_findings(
    kept: list[Finding],
    *,
    llm_call: LLMCall,
    model: str,
    prompt_path: Path,
) -> JudgeResult:
    """Run the judge pass: one LLM call to score all candidate findings.

    Returns a ``JudgeResult`` with a modified copy of ``kept`` (downranked
    findings have lower confidence and ``demoted_to_body=True``) and the token
    usage for the judge LLM call. On any error the function returns the
    original ``kept`` unchanged with zero token counts (fail-soft).

    Args:
        kept: Final diff-scoped, rolled-up candidate findings (Phase 2.5 output).
        llm_call: The run's bound LLM call (same one used for agents).
        model: The standard (cheap) model to use for judging.
        prompt_path: Path to ``prompts/finding-judge.md``.
    """
    if not kept:
        return JudgeResult(findings=kept)

    try:
        system_prompt = prompt_path.read_text()
    except OSError as exc:
        logger.warning("judge: could not read prompt %s: %s", prompt_path, exc)
        return JudgeResult(findings=kept)

    user_message = _build_candidate_payload(kept)

    request = LLMRequest(
        model_id=model,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=4096,
        temperature=0.0,
    )

    try:
        response = await llm_call(request)
    except Exception as exc:
        logger.warning("judge: LLM call failed (fail-soft); keeping findings unchanged: %s", exc, exc_info=True)
        return JudgeResult(findings=kept)

    try:
        # Strip optional markdown code fence the LLM may wrap around the JSON.
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        verdicts: list[dict[str, object]] = parsed["verdicts"]
        if not isinstance(verdicts, list):
            raise ValueError(f"verdicts is not a list: {type(verdicts)}")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "judge: could not parse verdict response (fail-soft): %s; response=%r",
            exc, response.text[:500],
        )
        return JudgeResult(
            findings=kept,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            cache_read_tokens=response.cache_read_tokens,
        )

    modified, downrank_count = _apply_verdicts(kept, verdicts)
    if downrank_count:
        logger.info("judge: %d finding(s) downranked", downrank_count)
    return JudgeResult(
        findings=modified,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cache_creation_tokens=response.cache_creation_tokens,
        cache_read_tokens=response.cache_read_tokens,
    )
