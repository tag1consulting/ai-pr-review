"""Diff hunk → per-provider line positions.

Ports parse_valid_lines and parse_diff_new_lines from vcs/common.sh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TextIO

_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class LineRef:
    """A file + new-file line number pair."""

    file: str
    line: int


def parse_added_lines(diff_text: str) -> set[LineRef]:
    """Return LineRefs for every ADDED line (+) in the unified diff.

    Mirrors vcs/common.sh parse_valid_lines: only + lines are eligible
    anchors for inline review comments.
    """
    return _parse_diff(diff_text, include_context=False)


def parse_new_file_lines(diff_text: str) -> set[LineRef]:
    """Return LineRefs for every line present in the new file (+  and context).

    Mirrors vcs/common.sh parse_diff_new_lines: used for suggestion-range
    validation where context lines are acceptable anchors.
    """
    return _parse_diff(diff_text, include_context=True)


def parse_added_lines_io(fh: TextIO) -> set[LineRef]:
    return _parse_diff_io(fh, include_context=False)


def parse_new_file_lines_io(fh: TextIO) -> set[LineRef]:
    return _parse_diff_io(fh, include_context=True)


def _parse_diff(diff_text: str, *, include_context: bool) -> set[LineRef]:
    import io

    return _parse_diff_io(io.StringIO(diff_text), include_context=include_context)


def _parse_diff_io(fh: TextIO, *, include_context: bool) -> set[LineRef]:
    result: set[LineRef] = set()
    current_file = ""
    new_line = 0

    for raw_line in fh:
        line = raw_line.rstrip("\n")
        m = _DIFF_GIT.match(line)
        if m:
            current_file = m.group(2)
            new_line = 0
            continue

        # Skip +++ / --- diff headers
        if line.startswith("+++") or line.startswith("---"):
            continue

        m = _HUNK_HEADER.match(line)
        if m:
            new_line = int(m.group(1))
            continue

        if not current_file or new_line == 0:
            continue

        if line.startswith("+"):
            result.add(LineRef(current_file, new_line))
            new_line += 1
        elif line.startswith("-"):
            pass  # deleted line — no new_line increment
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            # context line
            if include_context:
                result.add(LineRef(current_file, new_line))
            new_line += 1

    return result
