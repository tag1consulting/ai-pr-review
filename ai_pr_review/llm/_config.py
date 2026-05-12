"""Runtime config helpers shared by all provider modules."""

from __future__ import annotations

import os
import sys


def _clamp_int(env_var: str, default: int, min_val: int, max_val: int) -> int:
    raw = os.environ.get(env_var, str(default)).strip()
    try:
        val = int(raw)
    except ValueError:
        print(
            f"WARNING: {env_var} '{raw}' is not a valid number; defaulting to {default}.",
            file=sys.stderr,
        )
        return default
    if val > max_val:
        print(
            f"WARNING: {env_var} '{val}' exceeds maximum ({max_val}); clamping.",
            file=sys.stderr,
        )
        return max_val
    return max(val, min_val)


def get_retry_count() -> int:
    return _clamp_int("LLM_RETRY_COUNT", default=3, min_val=0, max_val=10)


def get_retry_base_delay() -> int:
    return _clamp_int("LLM_RETRY_BASE_DELAY", default=2, min_val=0, max_val=30)


def resolve_temperature(raw: float, model_id: str) -> float | None:
    """Return None when the model doesn't accept temperature; else the clamped value."""
    lower = model_id.lower()
    if (
        "opus-4-7" in lower
        or "opus-4.7" in lower
        or lower.startswith("o1")
        or lower.startswith("o3")
        or lower.startswith("o4")
        or lower.startswith("gpt-5.5")
        or lower.startswith("gpt-5-")
        or lower == "gpt-5"
    ):
        return None
    return min(raw, 2.0)


def resolve_prompt_caching(provider: str) -> bool:
    """Resolve LLM_PROMPT_CACHING to a boolean for the given provider."""
    raw = os.environ.get("LLM_PROMPT_CACHING", "auto").strip().lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    if raw not in ("auto", ""):
        print(
            f"WARNING: LLM_PROMPT_CACHING='{raw}' is not a valid value; defaulting to auto.",
            file=sys.stderr,
        )
    # auto: enable for anthropic and bedrock-proxy
    return provider in ("anthropic", "bedrock-proxy")
