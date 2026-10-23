"""A bounded tool loop around one LLM call (#1088).

``run_tool_loop`` sends a request that carries tool definitions, runs the tool calls the
model makes against a ``ToolSession``, and sends the results back, until the model answers
or a limit forces a final turn. It sits above the provider layer, so it needs only a
function that takes an ``LLMRequest`` and returns an ``LLMResponse``. The caller's function
can add the response cache and the spend guard, and each turn goes through them.

Rules the loop keeps:

* Append only. An assistant turn goes back exactly as the model returned it (its ``content``
  blocks, thinking blocks included), and no earlier turn is edited. A provider that binds
  thinking to the conversation rejects or drops an edited history.
* Every ``tool_use`` block gets a ``tool_result`` in the next user turn, in one message, even
  when the call is refused or over the per-turn limit. The provider rejects a history with an
  unanswered tool call.
* The last turn is a forced final turn: ``tool_choice`` is ``none``, ``tools`` stay defined
  (the history holds tool blocks), and a short note asks for the final answer. A forced
  ``tool_choice`` of ``any`` or ``tool`` is never used, because newer models reject it.
* A refusal or any provider error raises out of the loop. The caller decides what to do. The
  exception gets ``loop_usage`` (a ``Usage`` of the calls that finished) and ``loop_turns``, so
  the spend of the earlier turns is not lost.
* Only the Anthropic provider supports tools. The other providers ignore the tool fields, and the
  Bedrock proxy refuses them.

The loop is not wired into the review pipeline and has no default-on switch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ai_pr_review.agents.tool_sandbox import ToolSession
from ai_pr_review.llm.base import LLMRequest, LLMResponse

_log = logging.getLogger(__name__)

LLMCall = Callable[[LLMRequest], Awaitable[LLMResponse]]

FINAL_NOTE = (
    "You can not use tools any more. Write your final answer now, including the json-findings block. "
    "Do not call a tool."
)
# Stop reasons that end a turn without a cut-off, as each provider spells it (Anthropic, OpenAI, Google).
# Anything else (max_tokens, pause_turn, length, MAX_TOKENS) is not an answer.
_COMPLETE_STOPS = ("end_turn", "stop_sequence", "stop", "STOP", "")
_OVER_LIMIT = "too many tool calls in one turn. Ask for fewer, or answer with what you have."


@dataclass(frozen=True)
class LoopLimits:
    max_turns: int = 8
    max_calls_per_turn: int = 4
    wall_seconds: float = 240.0


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    thinking_tokens: int = 0

    def add(self, response: LLMResponse) -> None:
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.cache_creation_tokens += response.cache_creation_tokens
        self.cache_read_tokens += response.cache_read_tokens
        self.thinking_tokens += response.thinking_tokens


@dataclass
class LoopResult:
    """The outcome of one loop. ``response`` is the last response, which carries the final answer."""

    response: LLMResponse
    turns: int
    stop: str  # answered, forced:<turn_limit|budget|time>, tool_use_after_final, malformed_tool_use, incomplete:<stop_reason>
    forced_final: bool
    usage: Usage = field(default_factory=Usage)
    messages: tuple[dict[str, Any], ...] = ()

    @property
    def text(self) -> str:
        return self.response.text

    def summary(self, session: ToolSession) -> dict[str, Any]:
        """Counts for telemetry and the eval log. No paths and no content."""
        return {"turns": self.turns, "stop": self.stop, "forced_final": self.forced_final,
                "model_calls": self.usage.calls, "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens, "thinking_tokens": self.usage.thinking_tokens,
                **session.summary()}


def _tool_result(use: dict[str, Any], text: str, is_error: bool) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": use["id"], "content": text, "is_error": is_error}


async def run_tool_loop(
    llm_call: LLMCall, request: LLMRequest, session: ToolSession,
    limits: LoopLimits | None = None, *, clock: Callable[[], float] = time.monotonic,
) -> LoopResult:
    """Run *request* with tools until the model answers or a limit forces the final turn."""
    limits = limits or LoopLimits()
    if not request.tools:
        raise ValueError("a tool loop needs request.tools")
    if limits.max_turns < 1 or limits.max_calls_per_turn < 1:
        raise ValueError("max_turns and max_calls_per_turn must be at least 1")
    messages: list[dict[str, Any]] = list(request.messages)
    usage = Usage()
    started = clock()
    turn = 0
    while True:
        turn += 1
        reason = ""
        if turn >= limits.max_turns:
            reason = "turn_limit"
        elif session.exhausted:
            reason = "budget"
        elif clock() - started > limits.wall_seconds:
            reason = "time"
        if reason:
            _log.info("tool loop: forcing the final turn (%s) at turn %d", reason, turn)
        if reason and messages and messages[-1]["role"] == "user":
            # The note goes into the user turn that carries the tool results, after them.
            last = messages[-1]["content"]
            blocks_so_far = [{"type": "text", "text": last}] if isinstance(last, str) else list(last)
            messages[-1] = {"role": "user", "content": [*blocks_so_far, {"type": "text", "text": FINAL_NOTE}]}
        try:
            response = await llm_call(replace(
                request, messages=tuple(messages), tool_choice="none" if reason else request.tool_choice))
        except (Exception, SystemExit) as exc:  # call_llm turns a provider error into SystemExit
            # The failed call has no response, so only the earlier turns are counted.
            exc.loop_usage = usage  # type: ignore[union-attr]
            exc.loop_turns = turn  # type: ignore[union-attr]
            raise
        usage.add(response)
        blocks = response.content or [{"type": "text", "text": response.text}]
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if response.stop_reason not in ("tool_use", *_COMPLETE_STOPS):
            # A cut-off turn has no complete answer and no complete tool call.
            _log.warning("tool loop: turn %d ended with stop_reason=%s", turn, response.stop_reason)
            return LoopResult(response, turn, f"incomplete:{response.stop_reason}", bool(reason), usage,
                              tuple(messages))
        if not uses or response.stop_reason != "tool_use":
            return LoopResult(response, turn, f"forced:{reason}" if reason else "answered", bool(reason), usage,
                              tuple(messages))
        if any(not isinstance(use.get("id"), str) or not use["id"] for use in uses):
            # No id means no tool_result can answer it, and the provider rejects a history with an unanswered call.
            _log.warning("tool loop: a tool_use block has no id")
            return LoopResult(response, turn, "malformed_tool_use", bool(reason), usage, tuple(messages))
        if reason:
            _log.warning("tool loop: the model asked for a tool on the forced final turn")
            return LoopResult(response, turn, "tool_use_after_final", True, usage, tuple(messages))
        messages.append({"role": "assistant", "content": blocks})
        results = []
        for index, use in enumerate(uses):
            if index >= limits.max_calls_per_turn:
                session.over_limit += 1
                results.append(_tool_result(use, _OVER_LIMIT, True))
                continue
            outcome = session.execute(str(use.get("name", "")), use.get("input"))
            results.append(_tool_result(use, outcome.text, outcome.is_error))
        if len(uses) > limits.max_calls_per_turn:
            _log.warning("tool loop: turn %d asked for %d calls, the limit is %d", turn, len(uses),
                         limits.max_calls_per_turn)
        messages.append({"role": "user", "content": results})
