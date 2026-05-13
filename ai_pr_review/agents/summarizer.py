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
    """Parse the LLM's markdown output into a typed SummarizerOutput."""
    sections = _split_top_level_sections(raw)
    summary_section = sections.get("summary", "")
    walkthrough_section = sections.get("walkthrough", "")
    diagram_section = sections.get("sequence diagrams", "") if include_diagram else ""

    summary_md = _extract_summary_text(summary_section)
    pr_type = _parse_pr_type(summary_section)
    effort = _parse_effort(summary_section)
    walkthrough = _parse_walkthrough(walkthrough_section)
    sequence_diagram = _parse_diagram(diagram_section) if include_diagram else None

    return SummarizerOutput(
        summary_md=summary_md,
        pr_type=pr_type,
        effort=effort,
        walkthrough=walkthrough,
        sequence_diagram=sequence_diagram,
    )


_SECTION_HEADING = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
_CODE_FENCE = re.compile(r"^(?P<indent>\s*)(?P<fence>`{3,}|~{3,})", re.MULTILINE)


def _strip_fenced_code(raw: str) -> str:
    """Mask content inside fenced code blocks, preserving byte offsets.

    Each masked character is replaced with a space (newlines preserved) so
    that regex positions computed on the masked string map 1:1 to positions
    in `raw`. This lets `_split_top_level_sections` safely slice `raw` using
    offsets derived from the masked scan.
    """
    out: list[str] = []
    fence: str | None = None
    for line in raw.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[: -len(newline)] if newline else line
        if fence is None:
            match = _CODE_FENCE.match(line)
            if match:
                fence = match.group("fence")[:3]
                out.append(line)  # keep the fence-opening line visible
                continue
            out.append(line)
        else:
            if body.lstrip().startswith(fence):
                fence = None
                out.append(line)  # keep the fence-closing line visible
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


def _parse_pr_type(section: str) -> PRType:
    match = _TYPE_LINE.search(section)
    if not match:
        print(
            "WARNING: summarizer output missing `**Type:**` line; defaulting to 'mixed'",
            file=sys.stderr,
        )
        return "mixed"
    pr_type = match.group(1).strip().lower()
    if pr_type not in _VALID_PR_TYPES:
        print(
            f"WARNING: summarizer pr_type {pr_type!r} not in allowed set; "
            "defaulting to 'mixed'",
            file=sys.stderr,
        )
        return "mixed"
    return pr_type  # type: ignore[return-value]


def _parse_effort(section: str) -> int:
    match = _EFFORT_LINE.search(section)
    if not match:
        print(
            "WARNING: summarizer output missing `**Effort:** N/5` line; defaulting to 3",
            file=sys.stderr,
        )
        return 3
    try:
        value = int(match.group(1))
    except ValueError:
        print(
            f"WARNING: summarizer effort {match.group(1)!r} unparseable; defaulting to 3",
            file=sys.stderr,
        )
        return 3
    if not 1 <= value <= 5:
        print(
            f"WARNING: summarizer effort {value} out of range [1,5]; defaulting to 3",
            file=sys.stderr,
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


def _parse_walkthrough(section: str) -> tuple[WalkthroughRow, ...]:
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
        # Rejoin any overflow cells (len > 3) into the summary so
        # unescaped pipes in summary prose don't truncate the row.
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
        print(
            f"WARNING: summarizer walkthrough: dropped {dropped} malformed row(s)",
            file=sys.stderr,
        )
    return tuple(rows)


_MERMAID_FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def _parse_diagram(section: str) -> str | None:
    """Extract and validate the Mermaid block from the sequence-diagram section."""
    if not section:
        return None
    match = _MERMAID_FENCE.search(section)
    if not match:
        return None
    block = match.group(1).rstrip()
    if not is_valid_mermaid(block):
        print(
            "WARNING: summarizer produced malformed Mermaid block; dropping",
            file=sys.stderr,
        )
        return None
    return block
