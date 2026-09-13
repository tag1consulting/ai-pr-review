"""Shared types for the LLM client layer."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass
class LLMRequest:
    model_id: str
    system_prompt: str
    user_message: str
    max_tokens: int = 4096
    temperature: float = 0.3
    # Resolved caching flag — True/False, never "auto" at call time.
    prompt_caching: bool = False
    # Optional shared, run-scoped system content (language profiles, feedback
    # addendum, etc.) that is byte-identical across every agent in a single
    # review.  When non-empty and prompt_caching is enabled on a provider that
    # supports multiple cache breakpoints (Anthropic, Bedrock), this content
    # leads the system field with its own cache_control marker so it caches
    # once per run and is read by every subsequent agent dispatch in that run.
    # Providers without multi-breakpoint caching prepend it to system_prompt.
    #
    # This is always the full "\n\n"-joined string of every run-shared
    # fragment, regardless of whether cache_blocks (below) is also populated
    # -- every provider except Anthropic/Bedrock reads only this field, and
    # Anthropic/Bedrock fall back to it too when cache_blocks is empty.
    system_prefix: str = ""
    # Same run-shared fragments as system_prefix, kept separate instead of
    # joined, ordered most-stable-first (#816). When non-empty and caching is
    # enabled, ai_pr_review.llm.anthropic/bedrock give each entry its OWN
    # cache_control breakpoint (up to 3, leaving Anthropic's 4th breakpoint
    # for the diff in `messages`) instead of one joined block sharing a
    # single cache lifetime -- so a change to a volatile fragment (e.g. the
    # feedback-loop addendum, which updates between reruns as the store
    # accumulates entries) does not invalidate the cache for a more-stable
    # fragment ahead of it (e.g. the PR-context block, stable for the PR's
    # whole lifetime). Anthropic's cache-hit check is prefix-cumulative: a
    # breakpoint's cache entry covers all `system` content from the start
    # through that breakpoint, so ordering matters -- most-stable content
    # must come first for the split to actually improve hit rate.
    #
    # Every provider OTHER than Anthropic/Bedrock ignores this field
    # entirely and continues to read only system_prefix, unaffected by its
    # introduction. dispatch.py is the only current populator; it builds
    # both fields from the same underlying parts list.
    cache_blocks: tuple[str, ...] = ()


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = ""
    thinking_tokens: int = 0
    # Raw request body captured when AI_PR_REVIEW_RECORD_DIR is set.
    _request_body: str = field(default="", repr=False)
    _response_body: str = field(default="", repr=False)
    _provider: str = field(default="", repr=False)

    def emit_stderr(self, model_id: str) -> None:
        """Emit TOKENS: and optional TRUNCATED:/THINKING: lines to stderr."""
        if self.thinking_tokens > 0:
            print(f"THINKING: {self.thinking_tokens} tokens (model={model_id})", file=sys.stderr)
        print(
            f"TOKENS: input={self.input_tokens} output={self.output_tokens} "
            f"cache_creation={self.cache_creation_tokens} "
            f"cache_read={self.cache_read_tokens} model={model_id}",
            file=sys.stderr,
        )
        if self.stop_reason in ("max_tokens", "length", "MAX_TOKENS"):
            print(
                f"WARNING: response truncated (stop_reason={self.stop_reason}); "
                "output may be incomplete",
                file=sys.stderr,
            )
            print("TRUNCATED:true", file=sys.stderr)
