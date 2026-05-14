"""Feedback injection — E3.S8.

Builds a ``<repo-feedback>`` XML block from stored FeedbackEntry objects and
injects it into the agent system prompt as an addendum.

Ranking heuristic (higher = more relevant):
  +2  if entry.file matches any changed file path (substring match)
  +1  if entry.rule_id is non-empty and entry.source matches the agent source

The block is token-budget-capped so it never crowding out the diff.
Token estimate: ``len(text) // 4`` (conservative 4-chars/token).
"""

from __future__ import annotations

import logging

from ai_pr_review.feedback.models import FeedbackEntry

logger = logging.getLogger(__name__)

_BLOCK_HEADER = "<repo-feedback>\n"
_BLOCK_FOOTER = "</repo-feedback>"
_ENTRY_TEMPLATE = (
    "<finding command={command!r} source={source!r} file={file!r}>"
    "{reason}"
    "</finding>"
)


def build_feedback_addendum(
    entries: list[FeedbackEntry],
    diff_text: str,
    *,
    max_tokens: int = 2048,
) -> str:
    """Build a ``<repo-feedback>`` addendum string from *entries*.

    Returns an empty string when there is nothing useful to inject.

    Parameters
    ----------
    entries:
        Recent FeedbackEntry objects (newest-first from the store).
    diff_text:
        The PR diff — used for file-path relevance scoring.
    max_tokens:
        Hard cap on the size of the injected block (approximate token count).
    """
    if not entries:
        return ""

    changed_paths = _extract_changed_paths(diff_text)
    scored = _rank(entries, changed_paths)

    lines: list[str] = [_BLOCK_HEADER]
    budget = max_tokens * 4  # chars budget (4 chars ≈ 1 token)
    used = len(_BLOCK_HEADER) + len(_BLOCK_FOOTER)

    for entry in scored:
        if not entry.reason and not entry.command:
            continue
        line = _render_entry(entry) + "\n"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)

    if len(lines) == 1:
        # Only the header — nothing fit or nothing scored
        return ""

    lines.append(_BLOCK_FOOTER)
    return "".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _extract_changed_paths(diff_text: str) -> list[str]:
    """Return file paths mentioned in unified diff ``+++ b/<path>`` lines."""
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:].strip())
    return paths


def _rank(
    entries: list[FeedbackEntry],
    changed_paths: list[str],
) -> list[FeedbackEntry]:
    """Return entries sorted by relevance score (descending), then age (newest first)."""

    def score(e: FeedbackEntry) -> int:
        s = 0
        if e.file and any(e.file in p or p in e.file for p in changed_paths):
            s += 2
        if e.rule_id:
            s += 1
        return s

    return sorted(entries, key=score, reverse=True)


def _render_entry(entry: FeedbackEntry) -> str:
    return _ENTRY_TEMPLATE.format(
        command=entry.command,
        source=entry.source,
        file=entry.file,
        reason=entry.reason,
    )
