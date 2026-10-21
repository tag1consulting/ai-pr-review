"""Tests for the bounded tool loop. A scripted model stands in for the provider, so no test calls the network."""

from __future__ import annotations

from typing import Any

import pytest

from ai_pr_review.agents.tool_loop import FINAL_NOTE, LoopLimits, run_tool_loop
from ai_pr_review.agents.tool_sandbox import TOOL_SPECS, Limits, MapStore, ToolSession
from ai_pr_review.llm._http import LLMContentError
from ai_pr_review.llm.base import LLMRequest, LLMResponse

_STORE = {"src/merge.py": b"PROXIMITY_LINES = 3\n\n\ndef near(a, b):\n    return abs(a - b) <= PROXIMITY_LINES\n"}
_FINAL = '```json-findings\n[]\n```'


def _use(tool_id: str, name: str = "grep", **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def _tool_turn(*uses: dict[str, Any], tokens: int = 10) -> LLMResponse:
    thinking = {"type": "thinking", "thinking": "", "signature": "SIG-1"}
    return LLMResponse(text="", input_tokens=tokens, output_tokens=5, stop_reason="tool_use",
                       content=[thinking, *uses])


def _answer(text: str = _FINAL, tokens: int = 10) -> LLMResponse:
    return LLMResponse(text=text, input_tokens=tokens, output_tokens=7, stop_reason="end_turn",
                       content=[{"type": "text", "text": text}])


class Scripted:
    """A model that returns the queued responses in order and records every request."""

    def __init__(self, *responses: LLMResponse | Exception) -> None:
        self.queue = list(responses)
        self.requests: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _request(**overrides: Any) -> LLMRequest:
    base: dict[str, Any] = {"model_id": "m", "system_prompt": "sys", "user_message": "diff", "tools": TOOL_SPECS}
    base.update(overrides)
    return LLMRequest(**base)


def _session(**limits: int) -> ToolSession:
    return ToolSession(MapStore(_STORE), Limits(**limits))


@pytest.mark.anyio
async def test_one_tool_turn_then_an_answer() -> None:
    model = Scripted(_tool_turn(_use("t1", pattern="PROXIMITY_LINES")), _answer())
    result = await run_tool_loop(model, _request(), _session())
    assert result.stop == "answered" and not result.forced_final and result.turns == 2
    assert result.text == _FINAL
    assert len(model.requests) == 2
    assert model.requests[0].messages == () and model.requests[0].tool_choice == ""
    second = model.requests[1].messages
    assert [m["role"] for m in second] == ["assistant", "user"]
    # The assistant turn goes back exactly as the model returned it, thinking block included.
    assert second[0]["content"][0] == {"type": "thinking", "thinking": "", "signature": "SIG-1"}
    answer = second[1]["content"][0]
    assert answer["type"] == "tool_result" and answer["tool_use_id"] == "t1" and answer["is_error"] is False
    assert "2 match(es)" in answer["content"]
    assert model.requests[1].tool_choice == "" and model.requests[1].tools == tuple(TOOL_SPECS)


@pytest.mark.anyio
async def test_parallel_calls_answer_in_one_user_message_in_order() -> None:
    model = Scripted(_tool_turn(_use("a", pattern="near"), _use("b", "read_file", path="src/merge.py")), _answer())
    await run_tool_loop(model, _request(), _session())
    user = model.requests[1].messages[1]
    assert user["role"] == "user"
    assert [b["tool_use_id"] for b in user["content"]] == ["a", "b"]
    assert all(b["type"] == "tool_result" for b in user["content"])


@pytest.mark.anyio
async def test_calls_over_the_per_turn_limit_still_get_a_result() -> None:
    uses = [_use(f"t{i}", pattern="near") for i in range(5)]
    model = Scripted(_tool_turn(*uses), _answer())
    session = _session()
    await run_tool_loop(model, _request(), session, LoopLimits(max_calls_per_turn=2))
    results = model.requests[1].messages[1]["content"]
    assert [b["tool_use_id"] for b in results] == [f"t{i}" for i in range(5)]
    assert [b["is_error"] for b in results] == [False, False, True, True, True]
    assert "too many tool calls" in results[2]["content"]
    assert session.calls == 2  # the refused calls were not run


@pytest.mark.anyio
async def test_bad_tool_requests_become_error_results_and_the_loop_goes_on() -> None:
    model = Scripted(_tool_turn(_use("a", "nope"), _use("b", "read_file", path="../etc/passwd")), _answer())
    result = await run_tool_loop(model, _request(), _session())
    results = model.requests[1].messages[1]["content"]
    assert [b["is_error"] for b in results] == [True, True]
    assert result.stop == "answered"


@pytest.mark.anyio
async def test_the_turn_limit_forces_a_final_turn_with_tools_defined_and_a_note() -> None:
    model = Scripted(_tool_turn(_use("t1", pattern="near")), _tool_turn(_use("t2", pattern="abs")), _answer())
    result = await run_tool_loop(model, _request(), _session(), LoopLimits(max_turns=3))
    assert result.stop == "forced:turn_limit" and result.forced_final and result.turns == 3
    last = model.requests[2]
    assert last.tool_choice == "none" and last.tools == tuple(TOOL_SPECS)
    final_user = last.messages[-1]
    assert [b["type"] for b in final_user["content"]] == ["tool_result", "text"]
    assert final_user["content"][-1]["text"] == FINAL_NOTE
    assert all(r.tool_choice == "" for r in model.requests[:2])


@pytest.mark.anyio
async def test_a_used_up_tool_budget_forces_the_next_turn_to_be_final() -> None:
    model = Scripted(_tool_turn(_use("t1", pattern="near")), _answer())
    result = await run_tool_loop(model, _request(), _session(max_calls=1))
    assert result.stop == "forced:budget" and model.requests[1].tool_choice == "none"


@pytest.mark.anyio
async def test_the_wall_clock_forces_a_final_turn() -> None:
    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    model = Scripted(_tool_turn(_use("t1", pattern="near")), _answer())
    result = await run_tool_loop(model, _request(), _session(), LoopLimits(wall_seconds=10), clock=lambda: next(ticks))
    assert result.stop == "forced:time" and model.requests[1].tool_choice == "none"


@pytest.mark.anyio
async def test_a_model_that_calls_a_tool_on_the_final_turn_ends_the_loop_without_running_it() -> None:
    model = Scripted(_tool_turn(_use("t1", pattern="near")), _tool_turn(_use("t2", pattern="abs")))
    session = _session()
    result = await run_tool_loop(model, _request(), session, LoopLimits(max_turns=2))
    assert result.stop == "tool_use_after_final" and result.turns == 2
    assert session.calls == 1 and len(model.queue) == 0


@pytest.mark.anyio
async def test_history_only_grows_and_the_request_is_not_changed() -> None:
    request = _request()
    model = Scripted(_tool_turn(_use("t1", pattern="near")), _tool_turn(_use("t2", pattern="abs")), _answer())
    await run_tool_loop(model, request, _session())
    sizes = [len(r.messages) for r in model.requests]
    assert sizes == [0, 2, 4]
    for earlier, later in zip(model.requests, model.requests[1:], strict=False):
        assert later.messages[:len(earlier.messages)] == earlier.messages  # no earlier turn is edited
    assert request.messages == () and request.tool_choice == ""


@pytest.mark.anyio
async def test_usage_adds_up_across_turns() -> None:
    model = Scripted(_tool_turn(_use("t1", pattern="near"), tokens=100), _answer(tokens=250))
    result = await run_tool_loop(model, _request(), _session())
    assert result.usage.calls == 2 and result.usage.input_tokens == 350 and result.usage.output_tokens == 12
    summary = result.summary(_session())
    assert summary["model_calls"] == 2 and summary["turns"] == 2 and summary["stop"] == "answered"


@pytest.mark.anyio
async def test_a_refusal_raises_out_of_the_loop() -> None:
    model = Scripted(_tool_turn(_use("t1", pattern="near")), LLMContentError("blocked (stop_reason=refusal)"))
    with pytest.raises(LLMContentError):
        await run_tool_loop(model, _request(), _session())


@pytest.mark.anyio
async def test_a_response_without_content_blocks_still_ends_the_loop() -> None:
    plain = LLMResponse(text=_FINAL, input_tokens=1, output_tokens=1, stop_reason="end_turn")
    result = await run_tool_loop(Scripted(plain), _request(), _session())
    assert result.stop == "answered" and result.turns == 1


@pytest.mark.anyio
async def test_the_loop_needs_tools_and_sane_limits() -> None:
    with pytest.raises(ValueError, match="request.tools"):
        await run_tool_loop(Scripted(), _request(tools=()), _session())
    with pytest.raises(ValueError, match="at least 1"):
        await run_tool_loop(Scripted(), _request(), _session(), LoopLimits(max_turns=0))
