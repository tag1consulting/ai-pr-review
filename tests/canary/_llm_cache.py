"""Record and replay model responses for harnesses that make billed calls.

Why this exists: a review pipeline is mostly deterministic code around one
non-deterministic, billed step, the model call. Changing the code after that
step (merge, suppression, a risk score, a label, an approval rule) does not need
a new model call to measure. Record each response once, and every later
experiment replays the saved text through the new code at no cost.

A response is keyed by the SHA-256 of everything that can change it: the
namespace (the provider), the model id, both system prompts, every cache block,
the user message, ``max_tokens``, and ``temperature``. A change to a reviewer
prompt, a diff, or a model id is a different key, so a stale response can never
be replayed for it. The ``prompt_caching`` flag is not part of the key because
it changes billing, not the answer.

Modes (``AI_EVAL_CACHE_MODE``, default ``auto``):

  auto    a hit is replayed at no cost, a miss makes the call and saves it
  record  always make the call and overwrite what was saved
  replay  a miss is an error and no call is ever made

Entries live in ``~/.cache/ai-pr-review-eval/llm-cache/`` (override with
``AI_EVAL_STATE_DIR``), outside the repository, so every worktree shares one
recording. They hold repository diffs and model output. Do not commit them, and
do not record client code into a directory that is synced anywhere.

Compose it outside the spend guard, so a hit never reserves or charges:

    cache.wrap(guard.wrap(real_call))
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402

LLMCall = Callable[[LLMRequest], Awaitable[LLMResponse]]

MODES = ("auto", "record", "replay")
_ENTRY_VERSION = 1


class CacheMiss(RuntimeError):
    """``replay`` mode found no saved response, and no call may be made."""


class CacheError(RuntimeError):
    """A saved entry is unreadable or does not match its key."""


def default_cache_dir() -> Path:
    override = os.environ.get("AI_EVAL_STATE_DIR", "").strip()
    base = Path(override).expanduser() if override else Path.home() / ".cache" / "ai-pr-review-eval"
    return base / "llm-cache"


def request_key(request: LLMRequest, namespace: str = "") -> str:
    """SHA-256 of every request field that can change the model's answer."""
    payload = {
        "namespace": namespace,
        "model_id": request.model_id,
        "system_prompt": request.system_prompt,
        "system_prefix": request.system_prefix,
        "cache_blocks": list(request.cache_blocks),
        "user_message": request.user_message,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    # Tool-loop turns (#1088). Added only when set, so the key of a request without them stays
    # exactly what it was and every saved response is still found. The tool results sit inside
    # ``messages``, so a changed file shows up in the key too.
    if request.messages:
        payload["messages"] = list(request.messages)
    if request.tools:
        payload["tools"] = list(request.tools)
    if request.tool_choice:
        payload["tool_choice"] = request.tool_choice
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    saved: int = 0


class LLMCache:
    def __init__(
        self, *, cache_dir: Path | None = None, mode: str = "auto", namespace: str = ""
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"cache mode must be one of {MODES}, not {mode!r}")
        self.mode = mode
        self.namespace = namespace
        self.cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self.stats = CacheStats()
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, namespace: str = "") -> LLMCache:
        return cls(mode=os.environ.get("AI_EVAL_CACHE_MODE", "auto").strip() or "auto",
                   namespace=namespace)

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def get(self, request: LLMRequest) -> LLMResponse | None:
        key = request_key(request, self.namespace)
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text())
            if entry.get("version") != _ENTRY_VERSION or entry.get("key") != key:
                raise CacheError("entry does not match its key")
            return LLMResponse(
                text=entry["text"],
                input_tokens=int(entry["input_tokens"]),
                output_tokens=int(entry["output_tokens"]),
                cache_creation_tokens=int(entry["cache_creation_tokens"]),
                cache_read_tokens=int(entry["cache_read_tokens"]),
                stop_reason=entry["stop_reason"],
                thinking_tokens=int(entry["thinking_tokens"]),
                content=list(entry.get("content", [])),
            )
        except (OSError, ValueError, KeyError, TypeError, CacheError) as exc:
            raise CacheError(f"cache entry {path} is unreadable: {exc}") from exc

    def put(self, request: LLMRequest, response: LLMResponse) -> None:
        key = request_key(request, self.namespace)
        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        entry = {
            "version": _ENTRY_VERSION,
            "key": key,
            "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "namespace": self.namespace,
            "model_id": request.model_id,
            "max_tokens": request.max_tokens,
            "text": response.text,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cache_creation_tokens": response.cache_creation_tokens,
            "cache_read_tokens": response.cache_read_tokens,
            "stop_reason": response.stop_reason,
            "thinking_tokens": response.thinking_tokens,
        }
        if response.content:
            entry["content"] = response.content  # a tool turn must replay with its tool_use blocks
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".entry.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(entry, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self.stats.saved += 1

    async def call(self, llm_call: LLMCall, request: LLMRequest) -> LLMResponse:
        if self.mode != "record":
            cached = self.get(request)
            if cached is not None:
                self.stats.hits += 1
                return cached
            if self.mode == "replay":
                self.stats.misses += 1
                raise CacheMiss(
                    f"no saved response for model {request.model_id!r} "
                    f"(key {request_key(request, self.namespace)[:12]}). "
                    "Run once in auto or record mode to save it."
                )
        self.stats.misses += 1
        response = await llm_call(request)
        self.put(request, response)
        return response

    def wrap(self, llm_call: LLMCall) -> LLMCall:
        async def cached(request: LLMRequest) -> LLMResponse:
            return await self.call(llm_call, request)

        return cached
