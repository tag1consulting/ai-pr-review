"""The response cache with tool-loop turns (#1088). No test makes a network call."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from ai_pr_review.llm.base import LLMRequest, LLMResponse

_CANARY = Path(__file__).resolve().parent.parent / "canary"


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _CANARY / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(_CANARY))
cache_mod = _load("_llm_cache")

# Keys computed by the code before tool use was added. If these change, every saved response is lost.
_GOLDEN_PLAIN = "a3380c33f0e64e81f81711e99f6600e1134cd16f55fb0d1c16da03c19cbf4160"
_GOLDEN_PREFIX = "83cbe4694e654d8d19d1668e4d6d93bdf0e1b3b351b6bfc2b350897c3484d664"


def _plain() -> LLMRequest:
    return LLMRequest(model_id="claude-sonnet-5-5", system_prompt="sys", user_message="diff")


def _prefixed() -> LLMRequest:
    return LLMRequest(model_id="claude-sonnet-5-5", system_prompt="SYSTEM", user_message="USER", max_tokens=32768,
                      temperature=0.3, system_prefix="PREFIX", cache_blocks=("A", "B"))


def test_the_key_of_a_request_without_tool_fields_is_unchanged() -> None:
    assert cache_mod.request_key(_plain(), "anthropic") == _GOLDEN_PLAIN
    assert cache_mod.request_key(_prefixed(), "anthropic+ctx-changed") == _GOLDEN_PREFIX


_TURN = ({"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "grep", "input": {}}]},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "R", "is_error": False}]})
_TOOLS = ({"name": "grep", "description": "d", "input_schema": {"type": "object"}},)


def _keyed(**kw: Any) -> str:
    base = _plain().__dict__ | kw
    return cache_mod.request_key(LLMRequest(**base), "anthropic")


def test_each_tool_field_changes_the_key() -> None:
    plain = _keyed()
    keys = {_keyed(messages=_TURN), _keyed(tools=_TOOLS), _keyed(tool_choice="none"), _keyed(tools=_TOOLS, tool_choice="none")}
    assert plain not in keys and len(keys) == 4


def test_a_different_tool_result_gives_a_different_key() -> None:
    changed = (_TURN[0], {"role": "user", "content": [{**_TURN[1]["content"][0], "content": "OTHER FILE TEXT"}]})
    assert _keyed(messages=_TURN, tools=_TOOLS) != _keyed(messages=changed, tools=_TOOLS)


def test_a_tool_turn_replays_with_its_content_blocks(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto", namespace="anthropic")
    request = LLMRequest(**(_plain().__dict__ | {"tools": _TOOLS}))
    blocks = [{"type": "thinking", "thinking": "", "signature": "S"},
              {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "x"}}]
    cache.put(request, LLMResponse(text="", input_tokens=3, output_tokens=4, stop_reason="tool_use", content=blocks))
    got = cache.get(request)
    assert got is not None and got.content == blocks and got.stop_reason == "tool_use" and got.text == ""


def test_an_entry_saved_before_content_existed_still_loads(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto", namespace="anthropic")
    request = _plain()
    cache.put(request, LLMResponse(text="old", input_tokens=1, output_tokens=2, stop_reason="end_turn"))
    path = cache._path(cache_mod.request_key(request, "anthropic"))
    assert '"content"' not in path.read_text()  # a text-only response writes no content field
    got = cache.get(request)
    assert got is not None and got.text == "old" and got.content == []


@pytest.mark.anyio
async def test_a_two_turn_loop_replays_from_the_cache_with_no_model_call(tmp_path: Path) -> None:
    from ai_pr_review.agents.tool_loop import run_tool_loop
    from ai_pr_review.agents.tool_sandbox import TOOL_SPECS, MapStore, ToolSession

    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto", namespace="anthropic")
    store = MapStore({"a.py": b"NEEDLE = 1\n"})
    request = LLMRequest(**(_plain().__dict__ | {"tools": TOOL_SPECS}))
    use = {"type": "tool_use", "id": "t1", "name": "grep", "input": {"pattern": "NEEDLE"}}
    script = [LLMResponse(text="", input_tokens=5, output_tokens=2, stop_reason="tool_use", content=[use]),
              LLMResponse(text="done", input_tokens=7, output_tokens=3, stop_reason="end_turn",
                          content=[{"type": "text", "text": "done"}])]
    calls = []

    async def provider(req: LLMRequest) -> LLMResponse:
        calls.append(req)
        return script[len(calls) - 1]

    first = await run_tool_loop(lambda r: cache.call(provider, r), request, ToolSession(store))
    assert first.text == "done" and len(calls) == 2

    async def no_call(req: LLMRequest) -> LLMResponse:
        raise AssertionError("the replay must not call the model")

    replay_cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="replay", namespace="anthropic")
    again = await run_tool_loop(lambda r: replay_cache.call(no_call, r), request, ToolSession(store))
    assert again.text == "done" and again.turns == 2 and replay_cache.stats.hits == 2
    # A changed file makes the second turn's request different, so the replay misses instead of lying.
    changed = ToolSession(MapStore({"a.py": b"NEEDLE = 2\n"}))
    with pytest.raises(cache_mod.CacheMiss):
        await run_tool_loop(lambda r: replay_cache.call(no_call, r), request, changed)
