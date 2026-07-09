"""Tests for ai_pr_review.llm.anthropic."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ai_pr_review.llm._http import LLMContentError, LLMError, LLMTransientError
from ai_pr_review.llm.anthropic import _parse_response, build_body_for_bedrock, call

from .conftest import fixture_text, make_request

API_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------

def test_parse_happy_path():
    resp = _parse_response(fixture_text("anthropic_happy.json"), {})
    assert resp.text == "Here is my review of the code."
    assert resp.input_tokens == 1024
    assert resp.output_tokens == 256
    assert resp.cache_creation_tokens == 0
    assert resp.cache_read_tokens == 0
    assert resp.stop_reason == "end_turn"


def test_parse_with_cache_read():
    resp = _parse_response(fixture_text("anthropic_cached.json"), {})
    assert resp.input_tokens == 128
    assert resp.output_tokens == 64
    assert resp.cache_read_tokens == 896


def test_parse_content_filter_raises():
    data = {
        "content": [],
        "stop_reason": "SAFETY",
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    with pytest.raises(LLMContentError, match="SAFETY"):
        _parse_response(json.dumps(data), {})


def test_parse_empty_text_raises():
    data = {
        "content": [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    with pytest.raises(LLMError, match="Could not extract"):
        _parse_response(json.dumps(data), {})


def test_parse_thinking_exhausted_raises_specific_diagnostic():
    """Regression lock for #592: adaptive thinking can consume max_tokens
    entirely before any text is produced, leaving a thinking-only content
    array with an empty `thinking` field (display: "omitted" on Sonnet 5).
    The error must name the actual cause (thinking exhaustion) rather than
    the generic "could not extract" message, and must not require reading
    a truncated response_text[:500] to diagnose.
    """
    resp_text = fixture_text("anthropic_thinking_exhausted.json")
    with pytest.raises(LLMError, match="exhausted max_tokens entirely on thinking"):
        _parse_response(resp_text, {"max_tokens": 16384})


def test_parse_thinking_exhausted_message_cites_token_counts():
    resp_text = fixture_text("anthropic_thinking_exhausted.json")
    with pytest.raises(LLMError, match=r"thinking_tokens=16384.*max_tokens=16384"):
        _parse_response(resp_text, {"max_tokens": 16384})


def test_parse_empty_text_without_thinking_exhaustion_uses_generic_message():
    """A non-thinking-related empty-text failure (e.g. stop_reason=end_turn
    with no content) must still surface the generic diagnostic, not be
    misattributed to thinking exhaustion.
    """
    data = {
        "content": [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    with pytest.raises(LLMError, match="Could not extract"):
        _parse_response(json.dumps(data), {})


def test_parse_populates_thinking_tokens_when_present():
    resp = _parse_response(fixture_text("anthropic_thinking_with_text.json"), {})
    assert resp.text == "Here is my review of the code."
    assert resp.thinking_tokens == 512
    # output_tokens is the inclusive authoritative total; must not be
    # double-counted by adding thinking_tokens on top (unlike google.py,
    # where candidatesTokenCount excludes thoughts).
    assert resp.output_tokens == 8686


def test_parse_thinking_tokens_defaults_to_zero_when_absent():
    resp = _parse_response(fixture_text("anthropic_happy.json"), {})
    assert resp.thinking_tokens == 0


# ---------------------------------------------------------------------------
# Request body construction
# ---------------------------------------------------------------------------

def test_body_caching_enabled():
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(
        model_id="claude-sonnet-4-6",
        system_prompt="Agent prompt.",
        user_message="Code context.",
    )
    body = _build_body(req, caching=True, extra={})

    assert body["model"] == "claude-sonnet-4-6"
    # system should be a list with two items
    assert isinstance(body["system"], list)
    assert len(body["system"]) == 2
    assert body["system"][0]["text"] == "Code context."
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][1]["text"] == "Agent prompt."
    # messages sentinel
    assert body["messages"] == [{"role": "user", "content": "Please perform your review now."}]


def test_body_caching_disabled():
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(system_prompt="Agent prompt.", user_message="Code context.")
    body = _build_body(req, caching=False, extra={})

    assert body["system"] == "Agent prompt."
    assert body["messages"] == [{"role": "user", "content": "Code context."}]


def test_body_caching_with_system_prefix_uses_two_breakpoints():
    """When system_prefix is non-empty and caching is enabled, the request
    must place the shared prefix first in `system` (with cache_control) and
    move the diff to `messages` (also with cache_control), so two distinct
    cache breakpoints are used.  This caches the run-shared system tail
    across every agent in the run.
    """
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(
        system_prompt="Per-agent prompt.",
        user_message="The diff content.",
        system_prefix="Shared run-scoped tail (governance + language profiles + feedback).",
    )
    body = _build_body(req, caching=True, extra={})

    # system has the shared prefix first (cached), per-agent prompt second.
    assert isinstance(body["system"], list)
    assert len(body["system"]) == 2
    assert body["system"][0]["text"].startswith("Shared run-scoped tail")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][1]["text"] == "Per-agent prompt."
    assert "cache_control" not in body["system"][1]

    # diff lives in messages with its own cache breakpoint.
    assert body["messages"][0]["role"] == "user"
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["text"] == "The diff content."
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_body_caching_without_system_prefix_preserves_legacy_layout():
    """An empty system_prefix must produce the byte-identical legacy layout
    (diff in system[0], stub user message) so existing callers that have not
    adopted system_prefix see no behavior change.
    """
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(
        system_prompt="Per-agent prompt.",
        user_message="The diff content.",
        system_prefix="",  # explicit empty
    )
    body = _build_body(req, caching=True, extra={})

    assert body["system"][0]["text"] == "The diff content."
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][1]["text"] == "Per-agent prompt."
    assert body["messages"] == [
        {"role": "user", "content": "Please perform your review now."}
    ]


def test_body_no_caching_with_system_prefix_concatenates():
    """When caching is disabled, system_prefix is concatenated ahead of the
    per-agent prompt so the model still sees identical content; the two
    breakpoints are simply not emitted.
    """
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(
        system_prompt="Per-agent prompt.",
        user_message="The diff.",
        system_prefix="Shared tail.",
    )
    body = _build_body(req, caching=False, extra={})

    assert body["system"] == "Shared tail.\n\nPer-agent prompt."
    assert body["messages"] == [{"role": "user", "content": "The diff."}]


def test_body_temperature_skipped_for_opus():
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(model_id="claude-opus-4-7-20250101", temperature=0.7)
    body = _build_body(req, caching=False, extra={})
    assert "temperature" not in body


def test_body_temperature_skipped_for_opus_4_8():
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(model_id="claude-opus-4-8-20250101", temperature=0.7)
    body = _build_body(req, caching=False, extra={})
    assert "temperature" not in body


def test_body_bedrock_extra_fields():
    req = make_request()
    body = build_body_for_bedrock(req, caching=False)
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in body


def test_body_effort_set_for_sonnet_5():
    """Regression lock for #592: Sonnet 5 must get output_config.effort="low"
    capped so adaptive thinking can't consume max_tokens entirely before any
    text is produced.
    """
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(model_id="claude-sonnet-5")
    body = _build_body(req, caching=False, extra={})
    assert body["output_config"] == {"effort": "low"}


def test_body_effort_omitted_for_sonnet_4_6():
    """Sonnet 4.6 predates output_config.effort; sending it risks a 400."""
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(model_id="claude-sonnet-4-6")
    body = _build_body(req, caching=False, extra={})
    assert "output_config" not in body


def test_body_effort_omitted_for_opus_4_8():
    """Opus 4.8 has thinking off by default; it doesn't have the #592
    failure mode and must not receive output_config.
    """
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(model_id="claude-opus-4-8")
    body = _build_body(req, caching=False, extra={})
    assert "output_config" not in body


def test_body_effort_set_for_bedrock_sonnet_5():
    """Regression lock: the bedrock-proxy model id must also get the cap,
    flowing through build_body_for_bedrock the same as direct Anthropic.
    """
    req = make_request(model_id="us.anthropic.claude-sonnet-5")
    body = build_body_for_bedrock(req, caching=False)
    assert body["output_config"] == {"effort": "low"}


# ---------------------------------------------------------------------------
# HTTP integration with respx
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with respx.mock:
        respx.post(API_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("anthropic_happy.json"))
        )
        req = make_request()
        resp = await call(req, caching=False)

    assert resp.text == "Here is my review of the code."
    assert resp.input_tokens == 1024


@pytest.mark.anyio
async def test_call_retries_on_429(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        return httpx.Response(200, text=fixture_text("anthropic_happy.json"))

    with respx.mock:
        respx.post(API_URL).mock(side_effect=side_effect)
        req = make_request()
        resp = await call(req, caching=False)

    assert resp.text == "Here is my review of the code."
    assert call_count == 3


@pytest.mark.anyio
async def test_call_429_exhausted_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "1")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    with respx.mock:
        respx.post(API_URL).mock(
            return_value=httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        )
        req = make_request()
        with pytest.raises(LLMTransientError):
            await call(req, caching=False)


@pytest.mark.anyio
async def test_call_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    req = make_request()
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        await call(req, caching=False)
