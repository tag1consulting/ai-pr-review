"""pr-summarizer orchestration and output parsing."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WalkthroughChange = Literal["Added", "Modified", "Deleted", "Renamed"]
PRType = Literal["feature", "bugfix", "refactor", "docs", "config", "test", "mixed"]
_VALID_CHANGES: frozenset[str] = frozenset({"Added", "Modified", "Deleted", "Renamed"})
_VALID_PR_TYPES: frozenset[str] = frozenset(
    {"feature", "bugfix", "refactor", "docs", "config", "test", "mixed"}
)

_MERMAID_ARROW = re.compile(r"-->>|->>|-->|->")
_DIAGRAM_ADDENDUM = """

## Optional: Sequence Diagram

After the walkthrough, when the PR contains a non-trivial control-flow change
(multi-step orchestration, new cross-service call, new pipeline stage), append
a `## Sequence Diagrams` section containing exactly one Mermaid `sequenceDiagram`
block. Focus on the single most significant call flow introduced or changed.

Skip the section entirely for docs-only, config, or value-only changes.

Format:

````markdown
## Sequence Diagrams

```mermaid
sequenceDiagram
    participant A as Caller
    participant B as Callee
    A->>B: method(args)
    B-->>A: result
```
````
"""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkthroughRow:
    file: str
    change: WalkthroughChange
    summary: str

    def __post_init__(self) -> None:
        if not self.file:
            raise ValueError("WalkthroughRow.file must be non-empty")
        if not self.summary:
            raise ValueError("WalkthroughRow.summary must be non-empty")
        if self.change not in _VALID_CHANGES:
            raise ValueError(
                f"WalkthroughRow.change must be one of {sorted(_VALID_CHANGES)}, "
                f"got {self.change!r}"
            )


@dataclass(frozen=True)
class SummarizerOutput:
    summary_md: str
    pr_type: PRType
    effort: int
    walkthrough: tuple[WalkthroughRow, ...]
    sequence_diagram: str | None
    parse_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.pr_type not in _VALID_PR_TYPES:
            raise ValueError(
                f"SummarizerOutput.pr_type must be one of {sorted(_VALID_PR_TYPES)}, "
                f"got {self.pr_type!r}"
            )
        if not 1 <= self.effort <= 5:
            raise ValueError(
                f"SummarizerOutput.effort must be in [1, 5], got {self.effort}"
            )

    @property
    def is_degraded(self) -> bool:
        """True when any parse fallback fired (defaults substituted)."""
        return bool(self.parse_warnings)


# ---------------------------------------------------------------------------
# Mermaid validation
# ---------------------------------------------------------------------------

def is_valid_mermaid(block: str) -> bool:
    """Syntactic smoke-check for a Mermaid sequenceDiagram block."""
    stripped = block.lstrip()
    if not stripped.startswith("sequenceDiagram"):
        return False
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if not body.strip():
        return False
    if "```" in body:
        return False
    return _MERMAID_ARROW.search(body) is not None


# ---------------------------------------------------------------------------
# Prompt/message assembly
# ---------------------------------------------------------------------------

def build_summarizer_system_prompt(base_path: Path, include_diagram: bool) -> str:
    """Read base prompt; append diagram addendum when requested."""
    base = base_path.read_text()
    if include_diagram:
        return base + _DIAGRAM_ADDENDUM
    return base


def build_summarizer_user_message(
    manifest: str, commit_log: str, diff_text: str
) -> str:
    """Structured user message combining manifest + commit log + diff."""
    return (
        f"## Manifest\n\n{manifest}\n\n"
        f"## Commit log\n\n{commit_log}\n\n"
        f"## Diff\n\n{diff_text}\n"
    )


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def parse_summarizer_output(raw: str, include_diagram: bool) -> SummarizerOutput:
    """Parse the LLM's markdown output into a typed SummarizerOutput.

    When parsing falls back to defaults (missing heading, unparseable effort,
    dropped walkthrough rows, malformed Mermaid), the reason is both logged
    to stderr AND recorded in the returned `parse_warnings` tuple. Callers
    can check `output.is_degraded` to decide whether to surface the
    degradation (e.g., skip posting, warn the user).
    """
    warnings: list[str] = []
    sections = _split_top_level_sections(raw)
    summary_section = sections.get("summary", "")
    walkthrough_section = sections.get("walkthrough", "")
    diagram_section = sections.get("sequence diagrams", "") if include_diagram else ""

    if not summary_section:
        warnings.append("missing-summary-section")
    if not walkthrough_section:
        warnings.append("missing-walkthrough-section")

    summary_md = _extract_summary_text(summary_section)
    pr_type = _parse_pr_type(summary_section, warnings)
    effort = _parse_effort(summary_section, warnings)
    walkthrough = _parse_walkthrough(walkthrough_section, warnings)
    sequence_diagram = (
        _parse_diagram(diagram_section, warnings) if include_diagram else None
    )

    return SummarizerOutput(
        summary_md=summary_md,
        pr_type=pr_type,
        effort=effort,
        walkthrough=walkthrough,
        sequence_diagram=sequence_diagram,
        parse_warnings=tuple(warnings),
    )


_SECTION_HEADING = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
_CODE_FENCE = re.compile(r"^(?P<indent>\s*)(?P<fence>`{3,}|~{3,})", re.MULTILINE)


def _is_closing_fence(line_body: str, opener: str) -> bool:
    """Return True if line_body closes a fence opened with `opener`.

    CommonMark: the closing fence must use the same fence character as the
    opener, be at least as long, be indented at most 3 spaces, and have
    nothing but optional trailing whitespace after the fence run.
    """
    char = opener[0]
    min_len = len(opener)
    stripped = line_body.lstrip()
    if not stripped.startswith(char):
        return False
    run_len = 0
    while run_len < len(stripped) and stripped[run_len] == char:
        run_len += 1
    if run_len < min_len:
        return False
    return stripped[run_len:].strip() == ""


def _strip_fenced_code(raw: str) -> str:
    """Mask content inside fenced code blocks, preserving byte offsets.

    Each masked character is replaced with a space (newlines preserved) so
    regex positions computed on the masked string map 1:1 to positions in
    `raw`. This lets `_split_top_level_sections` safely slice `raw` using
    offsets derived from the masked scan.

    CommonMark: a fenced block is closed by a line of the same fence
    character whose length is at least the opener's length. We preserve
    the full opener so a ````markdown`-opened block doesn't get closed
    by a triple-backtick line used inside it.
    """
    out: list[str] = []
    fence: str | None = None
    for line in raw.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[: -len(newline)] if newline else line
        if fence is None:
            match = _CODE_FENCE.match(line)
            if match:
                fence = match.group("fence")
                out.append(line)
            else:
                out.append(line)
        elif _is_closing_fence(body, fence):
            fence = None
            out.append(line)
        else:
            out.append(" " * len(body) + newline)
    return "".join(out)


def _split_top_level_sections(raw: str) -> dict[str, str]:
    """Split markdown on `## ` headings; return lowercased-heading → body.

    Fenced code blocks are masked before splitting so `##` lines inside
    code fences don't carve false sections. The returned section bodies
    are sliced from the original raw text (fences preserved).
    """
    masked = _strip_fenced_code(raw)
    sections: dict[str, str] = {}
    matches = list(_SECTION_HEADING.finditer(masked))
    for idx, match in enumerate(matches):
        name = match.group("name").strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        sections[name] = raw[start:end].strip()
    return sections


def _extract_summary_text(section: str) -> str:
    """Summary text = everything before the `**Type:**` line."""
    if not section:
        return ""
    lines: list[str] = []
    for line in section.splitlines():
        if line.lstrip().startswith("**Type:**"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


_TYPE_LINE = re.compile(r"^\*\*Type:\*\*\s*(\S+)", re.MULTILINE)
_EFFORT_LINE = re.compile(r"^\*\*Effort:\*\*\s*(\d+)\s*/\s*5", re.MULTILINE)


def _warn(msg: str, warnings: list[str], tag: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)
    warnings.append(tag)


def _parse_pr_type(section: str, warnings: list[str]) -> PRType:
    match = _TYPE_LINE.search(section)
    if not match:
        _warn(
            "summarizer output missing `**Type:**` line; defaulting to 'mixed'",
            warnings,
            "missing-type-line",
        )
        return "mixed"
    pr_type = match.group(1).strip().lower()
    if pr_type not in _VALID_PR_TYPES:
        _warn(
            f"summarizer pr_type {pr_type!r} not in allowed set; defaulting to 'mixed'",
            warnings,
            "unknown-pr-type",
        )
        return "mixed"
    return pr_type  # type: ignore[return-value]


def _parse_effort(section: str, warnings: list[str]) -> int:
    match = _EFFORT_LINE.search(section)
    if not match:
        _warn(
            "summarizer output missing `**Effort:** N/5` line; defaulting to 3",
            warnings,
            "missing-effort-line",
        )
        return 3
    try:
        value = int(match.group(1))
    except ValueError:
        _warn(
            f"summarizer effort {match.group(1)!r} unparseable; defaulting to 3",
            warnings,
            "unparseable-effort",
        )
        return 3
    if not 1 <= value <= 5:
        _warn(
            f"summarizer effort {value} out of range [1,5]; defaulting to 3",
            warnings,
            "effort-out-of-range",
        )
        return 3
    return value


def _split_table_row(stripped: str) -> list[str]:
    r"""Split a pipe-delimited table row, respecting backslash-escaped pipes.

    Markdown tables allow `\|` to represent a literal pipe inside a cell; this
    split rejoins cells when the preceding character is a backslash, then
    unescapes the sequence.
    """
    inner = stripped.strip("|")
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner) and inner[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _parse_walkthrough(
    section: str, warnings: list[str]
) -> tuple[WalkthroughRow, ...]:
    """Parse the pipe-delimited walkthrough table.

    Tolerates `\\|` escapes inside cell content. Merges extra cells (index ≥ 3)
    back into the summary with pipe separators so a summary like
    `| src/x.py | Modified | handles foo | bar |` becomes summary="handles foo | bar"
    rather than silently truncating at "handles foo".
    """
    rows: list[WalkthroughRow] = []
    dropped = 0
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = _split_table_row(stripped)
        if len(cells) < 3:
            dropped += 1
            continue
        if cells[0].lower() == "file" and cells[1].lower() == "change":
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        file_, change = cells[0], cells[1]
        summary = " | ".join(cells[2:]).strip()
        if not file_ or not change or not summary:
            dropped += 1
            continue
        if change not in _VALID_CHANGES:
            dropped += 1
            continue
        rows.append(
            WalkthroughRow(
                file=file_,
                change=change,  # type: ignore[arg-type]
                summary=summary,
            )
        )
    if dropped:
        _warn(
            f"summarizer walkthrough: dropped {dropped} malformed row(s)",
            warnings,
            f"walkthrough-rows-dropped={dropped}",
        )
    return tuple(rows)


_MERMAID_FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def _parse_diagram(section: str, warnings: list[str]) -> str | None:
    """Extract and validate the Mermaid block from the sequence-diagram section."""
    if not section:
        return None
    match = _MERMAID_FENCE.search(section)
    if not match:
        return None
    block = match.group(1).rstrip()
    if not is_valid_mermaid(block):
        _warn(
            "summarizer produced malformed Mermaid block; dropping",
            warnings,
            "malformed-mermaid",
        )
        return None
    return block
