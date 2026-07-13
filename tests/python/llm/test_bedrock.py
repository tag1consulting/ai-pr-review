"""Tests for ai_pr_review.llm.bedrock."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ai_pr_review.llm._http import LLMTransientError
from ai_pr_review.llm.bedrock import _parse_response, call

from .conftest import fixture_text, make_request

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_URL = "https://bedrock-proxy.example.com"
INVOKE_URL = f"{BEDROCK_URL}/model/us.anthropic.claude-sonnet-4-6/invoke"


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------

def test_parse_happy_path():
    resp = _parse_response(fixture_text("bedrock_happy.json"), {})
    assert resp.text == "Bedrock review result."
    assert resp.input_tokens == 512
    assert resp.output_tokens == 128


def test_parse_content_filter_raises():
    data = {
        "content": [],
        "stop_reason": "SAFETY",
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    from ai_pr_review.llm._http import LLMContentError

    with pytest.raises(LLMContentError, match="SAFETY"):
        _parse_response(json.dumps(data), {})


# ---------------------------------------------------------------------------
# Request body has anthropic_version field
# ---------------------------------------------------------------------------

def test_body_has_anthropic_version():
    from ai_pr_review.llm.anthropic import build_body_for_bedrock

    req = make_request(model_id=MODEL_ID)
    body = build_body_for_bedrock(req, caching=False)
    assert body.get("anthropic_version") == "bedrock-2023-05-31"
    # Model must NOT be in body (it goes in the URL for Bedrock)
    assert "model" not in body


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", BEDROCK_URL)
    monkeypatch.setenv("BEDROCK_API_KEY", "test-key")

    with respx.mock:
        respx.post(INVOKE_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("bedrock_happy.json"))
        )
        req = make_request(model_id=MODEL_ID)
        resp = await call(req)

    assert resp.text == "Bedrock review result."


@pytest.mark.anyio
async def test_call_retries_on_429(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", BEDROCK_URL)
    monkeypatch.setenv("BEDROCK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    call_count = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        return httpx.Response(200, text=fixture_text("bedrock_happy.json"))

    with respx.mock:
        respx.post(INVOKE_URL).mock(side_effect=side_effect)
        req = make_request(model_id=MODEL_ID)
        resp = await call(req)

    assert resp.text == "Bedrock review result."
    assert call_count == 3


@pytest.mark.anyio
async def test_call_429_exhausted_raises(monkeypatch):
    monkeypatch.setenv("BEDROCK_API_URL", BEDROCK_URL)
    monkeypatch.setenv("BEDROCK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RETRY_COUNT", "1")
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")

    with respx.mock:
        respx.post(INVOKE_URL).mock(
            return_value=httpx.Response(429, text=fixture_text("rate_limit_429.json"))
        )
        req = make_request(model_id=MODEL_ID)
        with pytest.raises(LLMTransientError):
            await call(req)


@pytest.mark.anyio
async def test_call_strips_whitespace_from_key_and_url(monkeypatch):
    # #600: trailing whitespace on either secret/variable must not reach the
    # outgoing Authorization header or get baked into the constructed URL.
    monkeypatch.setenv("BEDROCK_API_URL", f"  {BEDROCK_URL}\t")
    monkeypatch.setenv("BEDROCK_API_KEY", "\ntest-key ")

    with respx.mock:
        route = respx.post(INVOKE_URL).mock(
            return_value=httpx.Response(200, text=fixture_text("bedrock_happy.json"))
        )
        req = make_request(model_id=MODEL_ID)
        await call(req)

    sent_headers = route.calls.last.request.headers
    assert sent_headers["Authorization"] == "Bearer test-key"
