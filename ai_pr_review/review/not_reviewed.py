"""One short note that says what this review did not look at.

A reader sees "approved" or a list of findings. Without this note the reader
cannot tell a clean change from a change that was only partly reviewed. The note
lists four things that are known when the review starts:

- file patterns removed from the diff (lockfiles, vendor directories, and any
  user patterns),
- agents that did not run because their trigger did not fire,
- analyzers that a fork run skips because they read configuration from the pull request, and
- an incremental review, which covers only the commits since the last review.

The note is plain text, built from names and patterns the tool itself holds.
It never contains text from the diff.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Pathspec prefix that diff/compute.py adds to every exclude pattern.
_PATHSPEC_PREFIX = ":!"
# Keep the note short. A long user pattern list is cut and the count is shown.
_MAX_PATTERNS = 8
_MAX_AGENTS = 8


def _clean(pattern: str) -> str:
    return pattern[len(_PATHSPEC_PREFIX):] if pattern.startswith(_PATHSPEC_PREFIX) else pattern


def _listed(items: Sequence[str], limit: int) -> str:
    shown = ", ".join(f"`{i}`" for i in items[:limit])
    extra = len(items) - limit
    return f"{shown} and {extra} more" if extra > 0 else shown


def build_not_reviewed_notice(
    *,
    excluded_patterns: Iterable[str],
    skipped_agents: Iterable[str],
    is_incremental: bool,
    skipped_analyzers: Iterable[str] = (),
) -> str:
    """Return the note, or an empty string when nothing was left out."""
    patterns = [p for p in (_clean(p) for p in excluded_patterns) if p]
    agents = sorted(set(skipped_agents))
    parts: list[str] = []
    if patterns:
        parts.append(f"files matching {_listed(patterns, _MAX_PATTERNS)}")
    if agents:
        parts.append(
            f"no run of {_listed(agents, _MAX_AGENTS)} (the trigger for "
            f"{'this agent' if len(agents) == 1 else 'these agents'} did not fire)"
        )
    analyzers = sorted(set(skipped_analyzers))
    if analyzers:
        parts.append(
            f"no run of the static analyzers {_listed(analyzers, _MAX_AGENTS)} (a fork run skips "
            "analyzers that read configuration from the pull request)"
        )
    if is_incremental:
        parts.append("changes before the last review (this is an incremental review)")
    if not parts:
        return ""
    return "> **Not reviewed:** " + ". ".join(p[0].upper() + p[1:] for p in parts) + "."
