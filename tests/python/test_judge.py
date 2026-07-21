"""Tests for ai_pr_review.findings.judge (Story 7-3, #360 remainder)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ai_pr_review.findings.judge import (
    JUDGE_DOWNRANK_AMOUNT,
    JudgeResult,
    _apply_verdicts,
    judge_findings,
)
from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest, LLMResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    *,
    severity: str = "High",
    confidence: int = 80,
    file: str = "app.py",
    line: int = 42,
    finding: str = "SQL injection via f-string",
    sources: list[str] | None = None,
    corroborated: bool = False,
    out_of_diff: bool = False,
) -> Finding:
    return Finding(
        severity=severity,
        confidence=confidence,
        file=file,
        line=line,
        finding=finding,
        source="security-reviewer",
        sources=sources or ["security-reviewer"],
        corroborated=corroborated,
        out_of_diff=out_of_diff,
    )


def _verdict_response(verdicts: list[dict[str, object]]) -> LLMResponse:
    import json
    return LLMResponse(
        text=json.dumps({"verdicts": verdicts}),
        input_tokens=50,
        output_tokens=20,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# JUDGE_DOWNRANK_AMOUNT constant
# ---------------------------------------------------------------------------

def test_judge_downrank_amount_is_15() -> None:
    assert JUDGE_DOWNRANK_AMOUNT == 15


# ---------------------------------------------------------------------------
# _apply_verdicts unit tests
# ---------------------------------------------------------------------------

def test_apply_verdicts_keep_unchanged() -> None:
    f = _finding(confidence=80)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "keep", "reason": "clear"}])
    assert result[0] is f
    assert count == 0


def test_apply_verdicts_downrank_lowers_confidence() -> None:
    f = _finding(confidence=80)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "downrank", "reason": "vague"}])
    assert result[0].confidence == 80 - JUDGE_DOWNRANK_AMOUNT
    assert result[0].demoted_to_body is True
    # out_of_diff is a distinct flag (set only by apply_diff_scope, always
    # paired with a Low severity cap — see findings/models.py). The judge
    # must never touch it: downrank changes placement, not risk. Fixes #622.
    assert result[0].out_of_diff is False
    assert result[0].severity == "High"  # downrank never changes severity
    assert count == 1


def test_apply_verdicts_downrank_capped_at_zero() -> None:
    f = _finding(confidence=5)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "downrank", "reason": "vague"}])
    assert result[0].confidence == 0
    assert result[0].demoted_to_body is True
    assert result[0].out_of_diff is False


def test_apply_verdicts_downrank_on_already_out_of_diff_finding_is_safe() -> None:
    """Regression test: the judge pass runs after apply_diff_scope in the
    pipeline (see orchestrate.py), so a finding can legitimately reach
    _apply_verdicts already carrying out_of_diff=True, severity="Low" (set
    by apply_diff_scope's own invariant). A downrank verdict on such a
    finding sets demoted_to_body=True on top, producing a Finding with BOTH
    out_of_diff=True and demoted_to_body=True -- a combination not exercised
    by any other test. This must degrade safely: compute_headline()
    (vcs/_body.py) excludes any out_of_diff finding from the headline
    regardless of demoted_to_body, so the combination can only affect a
    finding already capped to Low, never mask a High/Critical. This test
    pins the actual _apply_verdicts behavior for that combined state rather
    than leaving it implicit."""
    f = _finding(confidence=80, severity="Low", out_of_diff=True)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "downrank", "reason": "vague"}])
    assert result[0].demoted_to_body is True
    assert result[0].out_of_diff is True, (
        "downrank must not clear a pre-existing out_of_diff flag"
    )
    assert result[0].severity == "Low", (
        "downrank must never change severity, regardless of out_of_diff state"
    )
    assert count == 1


def test_apply_verdicts_corroborated_exempt_from_downrank() -> None:
    f = _finding(confidence=80, corroborated=True)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "downrank", "reason": "vague"}])
    assert result[0] is f
    assert result[0].confidence == 80
    assert result[0].demoted_to_body is False
    assert result[0].out_of_diff is False
    assert count == 0


def test_apply_verdicts_missing_id_defaults_to_keep() -> None:
    f = _finding(confidence=80)
    result, count = _apply_verdicts([f], [])  # no verdict for id=0
    assert result[0] is f
    assert count == 0


def test_apply_verdicts_multiple_findings_mixed() -> None:
    weak = _finding(confidence=70, finding="vague finding")
    strong = _finding(confidence=90, finding="SQL injection", corroborated=True)
    result, count = _apply_verdicts(
        [weak, strong],
        [
            {"id": 0, "verdict": "downrank", "reason": "vague"},
            {"id": 1, "verdict": "downrank", "reason": "would downrank"},
        ],
    )
    assert result[0].confidence == 70 - JUDGE_DOWNRANK_AMOUNT
    assert result[0].demoted_to_body is True
    assert result[1].confidence == 90  # corroborated → exempt
    assert result[1].demoted_to_body is False
    assert count == 1


# ---------------------------------------------------------------------------
# judge_findings async tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_judge_keep_unchanged(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    call = AsyncMock(return_value=_verdict_response([{"id": 0, "verdict": "keep", "reason": "clear"}]))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert isinstance(result, JudgeResult)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == 80
    assert result.findings[0].out_of_diff is False
    call.assert_called_once()


@pytest.mark.anyio
async def test_judge_returns_token_counts_on_success(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    call = AsyncMock(return_value=_verdict_response([{"id": 0, "verdict": "keep", "reason": "clear"}]))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result.input_tokens == 50
    assert result.output_tokens == 20


@pytest.mark.anyio
async def test_judge_downrank_lowers_confidence(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    call = AsyncMock(return_value=_verdict_response([{"id": 0, "verdict": "downrank", "reason": "vague"}]))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result.findings[0].confidence == 80 - JUDGE_DOWNRANK_AMOUNT
    assert result.findings[0].demoted_to_body is True
    assert result.findings[0].out_of_diff is False


@pytest.mark.anyio
async def test_judge_corroborated_exempt(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80, corroborated=True)
    call = AsyncMock(return_value=_verdict_response([{"id": 0, "verdict": "downrank", "reason": "vague"}]))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result.findings[0].confidence == 80
    assert result.findings[0].out_of_diff is False


@pytest.mark.anyio
async def test_judge_fail_soft_on_llm_error(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    call = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert isinstance(result, JudgeResult)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == 80  # unchanged
    assert result.input_tokens == 0
    assert result.output_tokens == 0


@pytest.mark.anyio
async def test_judge_fail_soft_on_bad_json(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    bad_response = LLMResponse(text="NOT JSON AT ALL", input_tokens=5, output_tokens=5)
    call = AsyncMock(return_value=bad_response)

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == 80  # unchanged
    # Token counts are captured even on parse failure — the LLM call did complete.
    assert result.input_tokens == 5
    assert result.output_tokens == 5


@pytest.mark.anyio
async def test_judge_fail_soft_on_missing_verdicts_key(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    bad_response = LLMResponse(text='{"wrong_key": []}', input_tokens=5, output_tokens=5)
    call = AsyncMock(return_value=bad_response)

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result.findings[0].confidence == 80
    assert result.input_tokens == 5


@pytest.mark.anyio
async def test_judge_empty_input_no_llm_call(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    call = AsyncMock()
    result = await judge_findings([], llm_call=call, model="claude-test", prompt_path=prompt)

    assert isinstance(result, JudgeResult)
    assert result.findings == []
    assert result.input_tokens == 0
    call.assert_not_called()


@pytest.mark.anyio
async def test_judge_fail_soft_on_verdicts_not_a_list(tmp_path: Path) -> None:
    """Covers the isinstance(verdicts, list) guard — ValueError triggers fail-soft."""
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    bad_response = LLMResponse(text='{"verdicts": "not-a-list"}', input_tokens=5, output_tokens=5)
    call = AsyncMock(return_value=bad_response)

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == 80  # unchanged — fail-soft
    assert result.input_tokens == 5


def test_apply_verdicts_malformed_entry_skipped() -> None:
    """Malformed verdict entries are skipped; valid entries still apply."""
    weak = _finding(confidence=70, finding="weak finding")
    strong = _finding(confidence=90, finding="strong finding")
    result, count = _apply_verdicts(
        [weak, strong],
        [
            {"id": "not-an-int", "verdict": "downrank"},  # malformed — skipped
            {"id": 1, "verdict": "downrank", "reason": "vague"},  # valid
        ],
    )
    assert result[0].confidence == 70  # malformed entry skipped → keep
    assert result[0].demoted_to_body is False
    assert result[1].confidence == 90 - JUDGE_DOWNRANK_AMOUNT  # valid entry applied
    assert result[1].demoted_to_body is True
    assert count == 1


@pytest.mark.anyio
async def test_judge_missing_prompt_file_fail_soft(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nonexistent.md"
    f = _finding(confidence=80)
    call = AsyncMock()

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=nonexistent)
    assert isinstance(result, JudgeResult)
    assert len(result.findings) == 1
    assert result.findings[0].confidence == 80
    assert result.input_tokens == 0
    call.assert_not_called()


@pytest.mark.anyio
async def test_judge_llm_request_has_correct_model(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding()
    captured: list[LLMRequest] = []

    async def capture_call(req: LLMRequest) -> LLMResponse:
        captured.append(req)
        return _verdict_response([{"id": 0, "verdict": "keep", "reason": "ok"}])

    await judge_findings([f], llm_call=capture_call, model="claude-haiku-test", prompt_path=prompt)
    assert captured[0].model_id == "claude-haiku-test"
    assert captured[0].temperature == 0.0
    assert captured[0].max_tokens == 4096
