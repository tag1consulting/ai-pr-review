"""LLM candidate-finding judge pass — Story 7-3 (#360 remainder).

After the findings pipeline has merged, suppressed, and scoped findings,
this module sends a single compact LLM call that asks a cheap model to
evaluate each finding's quality and return a ``keep`` or ``downrank``
verdict per finding id.

Design constraints (from user decisions, session 2026-06-22):
- ``drop`` is NOT a valid verdict: the judge never removes a finding.
  A false positive is better than a missed vulnerability.
- ``downrank`` lowers confidence and routes the finding to the review body
  (sets ``out_of_diff=True``) so it is still visible but does not appear
  as an inline PR comment.
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
from pathlib import Path
from typing import Literal

from ai_pr_review.agents.dispatch import LLMCall
from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest

logger = logging.getLogger(__name__)

JUDGE_DOWNRANK_AMOUNT: int = 15

JudgeVerdict = Literal["keep", "downrank"]


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
      set out_of_diff=True so the finding routes to the review body.
    - ``keep`` → unchanged.
    - Missing verdict id defaults to ``keep``.
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
            # model_copy skips validator re-runs; safe here — only confidence and
            # out_of_diff are updated and neither has cross-field validation.
            result.append(finding.model_copy(update={"confidence": new_confidence, "out_of_diff": True}))
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
) -> list[Finding]:
    """Run the judge pass: one LLM call to score all candidate findings.

    Returns a modified copy of ``kept`` with downranked findings having
    lower confidence and ``out_of_diff=True``. The original list is never
    mutated. On any error the function returns ``kept`` unchanged (fail-soft).

    Args:
        kept: Final diff-scoped, rolled-up candidate findings (Phase 2.5 output).
        llm_call: The run's bound LLM call (same one used for agents).
        model: The standard (cheap) model to use for judging.
        prompt_path: Path to ``prompts/finding-judge.md``.
    """
    if not kept:
        return kept

    try:
        system_prompt = prompt_path.read_text()
    except OSError as exc:
        logger.warning("judge: could not read prompt %s: %s", prompt_path, exc)
        return kept

    user_message = _build_candidate_payload(kept)

    request = LLMRequest(
        model_id=model,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=1024,
        temperature=0.0,
    )

    try:
        response = await llm_call(request)
    except Exception as exc:
        logger.warning("judge: LLM call failed (fail-soft); keeping findings unchanged: %s", exc, exc_info=True)
        return kept

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
        return kept

    modified, downrank_count = _apply_verdicts(kept, verdicts)
    if downrank_count:
        logger.info("judge: %d finding(s) downranked", downrank_count)
    return modified
