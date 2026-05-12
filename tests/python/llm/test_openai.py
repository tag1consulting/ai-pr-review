"""Tests for ai_pr_review.llm.openai."""

from __future__ import annotations

import httpx
import pytest
import respx

from ai_pr_review.llm._http import LLMTransientError
from ai_pr_review.llm.openai import _build_body, _parse_response, call

from .conftest import fixture_text, make_request

API_URL = "https://api.openai.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------

def test_parse_happy_path():
    resp = _parse_response(fixture_text("openai_happy.json"), {})
    assert resp.text == "Here is my OpenAI review."
    assert resp.output_tokens == 256
    # No cached tokens: uncached = 1024 - 0
    assert resp.input_tokens == 1024
    assert resp.cache_read_tokens == 0


def test_parse_with_cache():
    resp = _parse_response(fixture_text("openai_cached.json"), {})
    # prompt_tokens=1024, cached=768 → uncached=256
    assert resp.input_tokens == 256
    assert resp.cache_read_tokens == 768


# ---------------------------------------------------------------------------
# Request body construction
# ---------------------------------------------------------------------------

def test_body_shared_cache_openai(monkeypatch):
    monkeypatch.setenv("LLM_PROMPT_CACHING", "auto")
    req = make_request(system_prompt="Agent instructions.", user_message="Code context.")
    body = _build_body(req, provider="openai")

    messages = body["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "===AGENT_INSTRUCTIONS===" in messages[0]["content"]
    assert "Code context." in messages[0]["content"]
    assert "Agent instructions." in messages[0]["content"]
    assert messages[1]["content"] == "Please perform your review now."
    # max_completion_tokens for first-party
    assert "max_completion_tokens" in body
    assert "max_tokens" not in body


def test_body_legacy_openai_compatible(monkeypatch):
    req = make_request(system_prompt="Prompt.", user_message="User msg.")
    body = _build_body(req, provider="openai-compatible")

    messages = body["messages"]
    assert messages[0] == {"role": "system", "content": "Prompt."}
    assert messages[1] == {"role": "user", "content": "User msg."}
    # openai-compatible uses max_tokens
    assert "max_tokens" in body
    assert "max_completion_tokens" not in body


def test_body_caching_disabled_openai(monkeypatch):
    monkeypatch.setenv("LLM_PROMPT_CACHING", "false")
    req = make_request(system_prompt="Prompt.", user_message="User msg.")
    body = _build_body(req, provider="openai")

    messages = body["messages"]
    # Legacy layout when caching disabled
    assert messages[0] == {"role": "system", "content": "Prompt."}
    assert messages[1] == {"role": "user", "content": "User msg."}


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with respx.mock:
        respx.post(API_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("openai_happy.json"))
        )
        req = make_request(model_id="gpt-5.4")
        resp = await call(req, provider="openai")

    assert resp.text == "Here is my OpenAI review."


@pytest.mark.anyio
async def test_call_retries_on_429(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        return httpx.Response(200, text=fixture_text("openai_happy.json"))

    with respx.mock:
        respx.post(API_URL).mock(side_effect=side_effect)
        req = make_request(model_id="gpt-5.4")
        resp = await call(req, provider="openai")

    assert resp.text == "Here is my OpenAI review."
    assert call_count == 3


@pytest.mark.anyio
async def test_call_429_exhausted_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "1")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    with respx.mock:
        respx.post(API_URL).mock(
            return_value=httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        )
        req = make_request(model_id="gpt-5.4")
        with pytest.raises(LLMTransientError):
            await call(req, provider="openai")
