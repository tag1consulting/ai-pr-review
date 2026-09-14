"""Context budget enforcement and injection — E3.S3.

Enforces a total token budget per agent for context-enrichment snippets.
Truncation order when over budget: same-file → same-package → repo-wide.
Injects context as a ``<symbol-context>…</symbol-context>`` block.

Token count is estimated as ``len(text) // 4`` (a conservative approximation
that slightly over-estimates to avoid budget overruns).

Gated by ``AI_CONTEXT_ENRICHMENT=1`` — callers must check the flag.
"""

from __future__ import annotations

from ai_pr_review.context.symbols import (
    PROXIMITY_DEFAULT,
    PROXIMITY_ORDER,
    Definition,
)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 chars ≈ 1 token, with a 10% safety margin.

    Known limitation (#848): the 4-chars-per-token ratio is calibrated for
    English-like source and prose. Real tokenizers assign roughly one token
    per CJK (Chinese/Japanese/Korean) character, not one token per four --
    so this heuristic can undercount a diff or comment block containing
    substantial CJK or other dense/non-Latin-script text by a factor of
    several times, in the *opposite* direction from every other bias this
    codebase's cost/budget estimates document (those over-estimate; this one
    can under-estimate for non-English-heavy content). A full CJK-aware
    tokenizer is out of scope here (this function has no dependency on any
    provider's real tokenizer, by design -- see callers' own docstrings);
    this is disclosed so callers relying on it for a hard budget or cost
    ceiling (``context/budget.py``'s own truncation, and
    ``review/cost_ceiling.py``'s pre-flight estimate) know where the number
    can run low, rather than assuming it is only ever conservative.
    """
    return int(len(text) / 4 * 1.1)


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
