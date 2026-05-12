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


def test_body_temperature_skipped_for_opus():
    from ai_pr_review.llm.anthropic import _build_body

    req = make_request(model_id="claude-opus-4-7-20250101", temperature=0.7)
    body = _build_body(req, caching=False, extra={})
    assert "temperature" not in body


def test_body_bedrock_extra_fields():
    req = make_request()
    body = build_body_for_bedrock(req, caching=False)
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in body


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
