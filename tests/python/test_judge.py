"""Tests for ai_pr_review.findings.judge (Story 7-3, #360 remainder)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ai_pr_review.findings.judge import (
    JUDGE_DOWNRANK_AMOUNT,
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
    assert result[0].out_of_diff is True
    assert count == 1


def test_apply_verdicts_downrank_capped_at_zero() -> None:
    f = _finding(confidence=5)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "downrank", "reason": "vague"}])
    assert result[0].confidence == 0
    assert result[0].out_of_diff is True


def test_apply_verdicts_corroborated_exempt_from_downrank() -> None:
    f = _finding(confidence=80, corroborated=True)
    result, count = _apply_verdicts([f], [{"id": 0, "verdict": "downrank", "reason": "vague"}])
    assert result[0] is f
    assert result[0].confidence == 80
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
    assert result[0].out_of_diff is True
    assert result[1].confidence == 90  # corroborated → exempt
    assert result[1].out_of_diff is False
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
    assert len(result) == 1
    assert result[0].confidence == 80
    assert result[0].out_of_diff is False
    call.assert_called_once()


@pytest.mark.anyio
async def test_judge_downrank_lowers_confidence(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    call = AsyncMock(return_value=_verdict_response([{"id": 0, "verdict": "downrank", "reason": "vague"}]))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result[0].confidence == 80 - JUDGE_DOWNRANK_AMOUNT
    assert result[0].out_of_diff is True


@pytest.mark.anyio
async def test_judge_corroborated_exempt(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80, corroborated=True)
    call = AsyncMock(return_value=_verdict_response([{"id": 0, "verdict": "downrank", "reason": "vague"}]))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result[0].confidence == 80
    assert result[0].out_of_diff is False


@pytest.mark.anyio
async def test_judge_fail_soft_on_llm_error(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    call = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert len(result) == 1
    assert result[0].confidence == 80  # unchanged


@pytest.mark.anyio
async def test_judge_fail_soft_on_bad_json(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    bad_response = LLMResponse(text="NOT JSON AT ALL", input_tokens=5, output_tokens=5)
    call = AsyncMock(return_value=bad_response)

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert len(result) == 1
    assert result[0].confidence == 80  # unchanged


@pytest.mark.anyio
async def test_judge_fail_soft_on_missing_verdicts_key(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    f = _finding(confidence=80)
    bad_response = LLMResponse(text='{"wrong_key": []}', input_tokens=5, output_tokens=5)
    call = AsyncMock(return_value=bad_response)

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=prompt)
    assert result[0].confidence == 80


@pytest.mark.anyio
async def test_judge_empty_input_no_llm_call(tmp_path: Path) -> None:
    prompt = tmp_path / "finding-judge.md"
    prompt.write_text("You are a finding-quality judge.")

    call = AsyncMock()
    result = await judge_findings([], llm_call=call, model="claude-test", prompt_path=prompt)

    assert result == []
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
    assert len(result) == 1
    assert result[0].confidence == 80  # unchanged — fail-soft


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
    assert result[0].out_of_diff is False
    assert result[1].confidence == 90 - JUDGE_DOWNRANK_AMOUNT  # valid entry applied
    assert result[1].out_of_diff is True
    assert count == 1


@pytest.mark.anyio
async def test_judge_missing_prompt_file_fail_soft(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nonexistent.md"
    f = _finding(confidence=80)
    call = AsyncMock()

    result = await judge_findings([f], llm_call=call, model="claude-test", prompt_path=nonexistent)
    assert len(result) == 1
    assert result[0].confidence == 80
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
