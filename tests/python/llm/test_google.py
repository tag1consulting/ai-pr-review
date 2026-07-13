"""Tests for ai_pr_review.llm.google."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ai_pr_review.llm._http import LLMContentError, LLMTransientError
from ai_pr_review.llm.google import _build_body, _parse_response, call

from .conftest import fixture_text, make_request

MODEL_ID = "gemini-2.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_ID}:generateContent"


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------

def test_parse_happy_path():
    resp = _parse_response(fixture_text("google_happy.json"), {})
    assert resp.text == "Here is my Gemini review."
    assert resp.input_tokens == 1024
    assert resp.output_tokens == 256
    assert resp.thinking_tokens == 0
    assert resp.cache_read_tokens == 0


def test_parse_thinking_tokens():
    resp = _parse_response(fixture_text("google_thinking.json"), {})
    # output = candidatesTokenCount + thoughtsTokenCount = 200 + 150 = 350
    assert resp.output_tokens == 350
    assert resp.thinking_tokens == 150


def test_parse_safety_filter_raises():
    data = {
        "candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}],
        "usageMetadata": {},
    }
    with pytest.raises(LLMContentError, match="SAFETY"):
        _parse_response(json.dumps(data), {})


def test_parse_empty_text_raises():
    data = {
        "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0},
    }
    from ai_pr_review.llm._http import LLMError

    with pytest.raises(LLMError, match="Could not extract"):
        _parse_response(json.dumps(data), {})


# ---------------------------------------------------------------------------
# Request body construction
# ---------------------------------------------------------------------------

def test_body_structure():
    req = make_request(model_id=MODEL_ID, system_prompt="Sys.", user_message="User.")
    body = _build_body(req)
    assert body["system_instruction"]["parts"][0]["text"] == "Sys."
    assert body["contents"][0]["parts"][0]["text"] == "User."
    assert body["generationConfig"]["maxOutputTokens"] == 4096


def test_body_temperature_skipped_for_opus():
    req = make_request(model_id="claude-opus-4-7")
    body = _build_body(req)
    assert "temperature" not in body["generationConfig"]


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    with respx.mock:
        respx.post(API_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("google_happy.json"))
        )
        req = make_request(model_id=MODEL_ID)
        resp = await call(req)

    assert resp.text == "Here is my Gemini review."


@pytest.mark.anyio
async def test_call_retries_on_429(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        return httpx.Response(200, text=fixture_text("google_happy.json"))

    with respx.mock:
        respx.post(API_URL).mock(side_effect=side_effect)
        req = make_request(model_id=MODEL_ID)
        resp = await call(req)

    assert resp.text == "Here is my Gemini review."
    assert call_count == 3


@pytest.mark.anyio
async def test_call_429_exhausted_raises(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "1")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    with respx.mock:
        respx.post(API_URL).mock(
            return_value=httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        )
        req = make_request(model_id=MODEL_ID)
        with pytest.raises(LLMTransientError):
            await call(req)


@pytest.mark.anyio
async def test_call_strips_whitespace_from_api_key(monkeypatch):
    # #600: a trailing newline/whitespace in the secret must not reach the
    # outgoing x-goog-api-key header.
    monkeypatch.setenv("GOOGLE_API_KEY", "  test-key\n")

    with respx.mock:
        route = respx.post(API_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("google_happy.json"))
        )
        req = make_request(model_id=MODEL_ID)
        await call(req)

    sent_headers = route.calls.last.request.headers
    assert sent_headers["x-goog-api-key"] == "test-key"
