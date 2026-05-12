"""Tests for ai_pr_review.llm.openai_compatible."""

from __future__ import annotations

import httpx
import pytest
import respx

from ai_pr_review.llm._http import LLMTransientError
from ai_pr_review.llm.openai import _build_body
from ai_pr_review.llm.openai_compatible import call

from .conftest import fixture_text, make_request

COMPAT_URL = "https://my-llm-proxy.example.com/v1"
COMPLETIONS_URL = f"{COMPAT_URL}/chat/completions"


def test_body_uses_max_tokens_not_max_completion_tokens():
    req = make_request()
    body = _build_body(req, provider="openai-compatible")
    assert "max_tokens" in body
    assert "max_completion_tokens" not in body


def test_body_uses_legacy_layout(monkeypatch):
    req = make_request(system_prompt="Prompt.", user_message="User msg.")
    body = _build_body(req, provider="openai-compatible")
    messages = body["messages"]
    assert messages[0] == {"role": "system", "content": "Prompt."}
    assert messages[1] == {"role": "user", "content": "User msg."}


@pytest.mark.anyio
async def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", COMPAT_URL)

    with respx.mock:
        respx.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("openai_happy.json"))
        )
        req = make_request()
        resp = await call(req)

    assert resp.text == "Here is my OpenAI review."


@pytest.mark.anyio
async def test_call_retries_on_429(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", COMPAT_URL)
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
        respx.post(COMPLETIONS_URL).mock(side_effect=side_effect)
        req = make_request()
        resp = await call(req)

    assert resp.text == "Here is my OpenAI review."
    assert call_count == 3


@pytest.mark.anyio
async def test_call_429_exhausted_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", COMPAT_URL)
    monkeypatch.setenv("LLM_RETRY_COUNT", "1")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    with respx.mock:
        respx.post(COMPLETIONS_URL).mock(
            return_value=httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        )
        req = make_request()
        with pytest.raises(LLMTransientError):
            await call(req)
