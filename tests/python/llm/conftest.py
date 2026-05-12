"""Shared fixtures and helpers for LLM provider tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text())


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def make_request(**kwargs: Any) -> Any:
    from ai_pr_review.llm.base import LLMRequest

    defaults = {
        "model_id": "claude-sonnet-4-6",
        "system_prompt": "You are a code reviewer.",
        "user_message": "Review this diff.",
        "max_tokens": 4096,
        "temperature": 0.3,
        "prompt_caching": False,
    }
    defaults.update(kwargs)
    return LLMRequest(**defaults)
