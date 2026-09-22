"""Context budget enforcement and injection — E3.S3.

Enforces a total token budget per agent for context-enrichment snippets.
Truncation order when over budget: same-file → same-package → repo-wide.
Injects context as a ``<symbol-context>…</symbol-context>`` block.

Token count is estimated with a character-count heuristic calibrated for
English-like source and prose: ``len(text) / 4`` for ASCII-range text, with
a 10% safety margin on top (a conservative over-estimate, to avoid budget
overruns). CJK/Hangul/kana text is charged at roughly 1 token per codepoint
instead (see ``estimate_tokens``'s docstring, #848 item 6) -- the 4-chars-
per-token ratio badly undercounts that text, since each codepoint there is
typically its own token or close to it under real BPE tokenizers.

Gated by ``AI_CONTEXT_ENRICHMENT=1`` — callers must check the flag.
"""

from __future__ import annotations

import re

from ai_pr_review.context.symbols import (
    PROXIMITY_DEFAULT,
    PROXIMITY_ORDER,
    Definition,
)

# Codepoint ranges where a 4-chars-per-token ratio badly undercounts: CJK
# ideographs (including compatibility/extension blocks and the SIP plane),
# Hangul (syllables and jamo), Hiragana/Katakana, and halfwidth/fullwidth
# forms. Matches the same conservative-over-estimate spirit as the base
# heuristic rather than trying to be tokenizer-exact.
_CJK_RANGES = (
    "ᄀ-ᇿ"     # Hangul Jamo
    "⺀-鿿"     # CJK Radicals through CJK Unified Ideographs
    "぀-ヿ"     # Hiragana, Katakana (overlaps the range above's gap)
    "가-힣"     # Hangul Syllables
    "豈-﫿"     # CJK Compatibility Ideographs
    "＀-￯"     # Halfwidth and Fullwidth Forms
    "\U00020000-\U0003ffff"  # CJK Unified Ideographs Extension B-G (SIP)
)
_CJK_RE = re.compile(f"[{_CJK_RANGES}]")


def estimate_tokens(text: str) -> int:
    """Estimate token count: ~4 chars/token for ASCII-range text, ~1
    token/codepoint for CJK/Hangul/kana text, with a 10% safety margin.

    For text with no CJK codepoints this is bit-identical to the previous
    ``int(len(text) / 4 * 1.1)`` formula -- ``_CJK_RE.sub`` returns the
    input unchanged when there's nothing to match, so every existing
    non-CJK caller and test is unaffected by construction. Splitting the
    count is a single C-level regex substitution plus a length diff, not a
    per-character Python loop, since this runs on entire diffs and inside
    a per-snippet truncation loop where an O(n) Python-level pass would be
    a real regression risk on large PRs.
    """
    non_cjk_len = len(_CJK_RE.sub("", text))
    cjk_len = len(text) - non_cjk_len
    return int((non_cjk_len / 4 + cjk_len) * 1.1)


# Backward-compatible alias for internal callers that used the private name.
_estimate_tokens = estimate_tokens


def _format_definition(d: Definition) -> str:
    return f"### {d.symbol} — {d.file}:{d.line}\n```\n{d.snippet}\n```"


def build_context_block(
    defs: list[Definition],
    *,
    max_tokens: int = 8192,
) -> str:
    """Build a ``<symbol-context>`` XML block from *defs*, respecting *max_tokens*.

    Returns an empty string when *defs* is empty or the budget would be
    entirely consumed by the wrapper alone.

    Truncation priority: same-file definitions are kept first, then
    same-package, then repo-wide, dropping from the end of each tier when
    over budget.
    """
    if not defs:
        return ""

    # Sort by proximity tier, then by file+line for determinism
    sorted_defs = sorted(
        defs,
        key=lambda d: (PROXIMITY_ORDER.get(d.proximity, PROXIMITY_DEFAULT), d.file, d.line),
    )

    wrapper_overhead = _estimate_tokens("<symbol-context>\n</symbol-context>")
    remaining = max_tokens - wrapper_overhead
    if remaining <= 0:
        return ""

    kept: list[str] = []
    for d in sorted_defs:
        formatted = _format_definition(d)
        cost = _estimate_tokens(formatted + "\n")
        if cost > remaining:
            continue  # skip this definition; try smaller ones later
        kept.append(formatted)
        remaining -= cost

    if not kept:
        return ""

    body = "\n\n".join(kept)
    return f"<symbol-context>\n{body}\n</symbol-context>"
