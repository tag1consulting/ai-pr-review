"""Tool-use fields in the Anthropic request and response (#1088).

The golden bodies in ``tests/python/data/anthropic_request_bodies.json`` were captured from the
client before tool use was added. A request that sets none of the new fields must still produce
exactly those bodies, so nothing that runs today can change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_pr_review.llm import anthropic as a
from ai_pr_review.llm._http import LLMContentError, LLMError
from ai_pr_review.llm.base import LLMRequest

_GOLDEN = json.loads((Path(__file__).resolve().parent.parent / "data" / "anthropic_request_bodies.json").read_text())


def _req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {"model_id": "claude-sonnet-5-5", "system_prompt": "SYSTEM", "user_message": "USER",
                            "max_tokens": 32768, "temperature": 0.3}
    base.update(kw)
    return LLMRequest(**base)


_CASES = {
    "caching_cache_blocks": (_req(system_prefix="A\n\nB", cache_blocks=("A", "B")), True),
    "caching_system_prefix": (_req(system_prefix="PREFIX"), True),
    "caching_legacy": (_req(), True),
    "no_caching_prefix": (_req(system_prefix="PREFIX"), False),
    "no_caching_plain": (_req(), False),
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_a_request_without_tool_fields_produces_the_same_body_as_before(name: str) -> None:
    request, caching = _CASES[name]
    assert a._build_body(request, caching=caching, extra={}) == _GOLDEN[name]


_TOOL = {"name": "grep", "description": "d", "input_schema": {"type": "object", "properties": {}}}
_HISTORY = (
    {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "S"},
                                      {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "x"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r", "is_error": False}]},
)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_tool_fields_are_added_after_the_first_user_turn_and_change_nothing_else(name: str) -> None:
    request, caching = _CASES[name]
    with_tools = a._build_body(
        _req(**{**request.__dict__, "messages": _HISTORY, "tools": (_TOOL,), "tool_choice": "none"}),
        caching=caching, extra={})
    golden = _GOLDEN[name]
    assert with_tools["messages"][:len(golden["messages"])] == golden["messages"]
    assert with_tools["messages"][len(golden["messages"]):] == list(_HISTORY)
    assert with_tools["tools"] == [_TOOL] and with_tools["tool_choice"] == {"type": "none"}
    rest = {k: v for k, v in with_tools.items() if k not in ("messages", "tools", "tool_choice")}
    assert rest == {k: v for k, v in golden.items() if k != "messages"}


def test_tool_choice_is_omitted_unless_set_and_forced_choices_are_refused() -> None:
    body = a._build_body(_req(tools=(_TOOL,)), caching=False, extra={})
    assert body["tools"] == [_TOOL] and "tool_choice" not in body
    assert a._build_body(_req(tools=(_TOOL,), tool_choice="auto"), caching=False, extra={})["tool_choice"] == {
        "type": "auto"}
    for forced in ("any", "tool", "required"):
        with pytest.raises(LLMError, match="tool_choice"):
            a._build_body(_req(tools=(_TOOL,), tool_choice=forced), caching=False, extra={})


def test_no_tools_means_no_tool_keys_even_if_a_choice_is_set() -> None:
    body = a._build_body(_req(tool_choice="none"), caching=False, extra={})
    assert "tools" not in body and "tool_choice" not in body


def _response(content: list[dict[str, Any]], stop_reason: str, **usage: int) -> str:
    return json.dumps({"content": content, "stop_reason": stop_reason,
                       "usage": {"input_tokens": 12, "output_tokens": 34, **usage}})


def test_a_tool_use_turn_with_no_text_is_a_valid_response_and_keeps_its_blocks() -> None:
    blocks = [{"type": "thinking", "thinking": "", "signature": "S"},
              {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "x"}}]
    parsed = a._parse_response(_response(blocks, "tool_use"), {"max_tokens": 1})
    assert parsed.text == "" and parsed.stop_reason == "tool_use" and parsed.content == blocks
    assert parsed.input_tokens == 12 and parsed.output_tokens == 34


def test_a_text_response_is_unchanged_and_also_keeps_its_blocks() -> None:
    blocks = [{"type": "text", "text": "hello"}, {"type": "text", "text": "second"}]
    parsed = a._parse_response(_response(blocks, "end_turn"), {"max_tokens": 1})
    assert parsed.text == "hello" and parsed.content == blocks


def test_an_empty_response_that_is_not_a_tool_turn_still_fails() -> None:
    with pytest.raises(LLMError, match="Could not extract response text"):
        a._parse_response(_response([], "end_turn"), {"max_tokens": 1})
    # stop_reason says tool_use but there is no tool_use block: still an error.
    with pytest.raises(LLMError, match="Could not extract response text"):
        a._parse_response(_response([{"type": "thinking", "thinking": "", "signature": "S"}], "tool_use"),
                          {"max_tokens": 1})


def test_a_refusal_still_raises_a_content_error() -> None:
    with pytest.raises(LLMContentError, match="refusal"):
        a._parse_response(_response([], "refusal"), {"max_tokens": 1})


def test_max_tokens_on_thinking_alone_keeps_its_clear_message() -> None:
    body = json.dumps({"content": [], "stop_reason": "max_tokens",
                       "usage": {"input_tokens": 1, "output_tokens": 9, "output_tokens_details": {"thinking_tokens": 9}}})
    with pytest.raises(LLMError, match="exhausted max_tokens entirely on thinking"):
        a._parse_response(body, {"max_tokens": 9})
