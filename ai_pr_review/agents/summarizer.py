"""pr-summarizer orchestration and output parsing."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WalkthroughChange = Literal["Added", "Modified", "Deleted", "Renamed"]
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


@dataclass(frozen=True)
class SummarizerOutput:
    summary_md: str
    pr_type: str
    effort: int
    walkthrough: list[WalkthroughRow]
    sequence_diagram: str | None


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


def _split_top_level_sections(raw: str) -> dict[str, str]:
    """Split markdown on `## ` headings; return lowercased-heading → body."""
    sections: dict[str, str] = {}
    matches = list(_SECTION_HEADING.finditer(raw))
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


def _parse_pr_type(section: str) -> str:
    match = _TYPE_LINE.search(section)
    if not match:
        return "mixed"
    pr_type = match.group(1).strip().lower()
    if pr_type not in _VALID_PR_TYPES:
        return "mixed"
    return pr_type


def _parse_effort(section: str) -> int:
    match = _EFFORT_LINE.search(section)
    if not match:
        return 3
    try:
        value = int(match.group(1))
    except ValueError:
        return 3
    if not 1 <= value <= 5:
        print(
            f"WARNING: summarizer effort {value} out of range [1,5]; defaulting to 3",
            file=sys.stderr,
        )
        return 3
    return value


def _parse_walkthrough(section: str) -> list[WalkthroughRow]:
    """Parse the pipe-delimited walkthrough table."""
    rows: list[WalkthroughRow] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0].lower() == "file" and cells[1].lower() == "change":
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        file_, change, summary = cells[0], cells[1], cells[2]
        if not file_ or not change or not summary:
            continue
        if change not in _VALID_CHANGES:
            continue
        rows.append(
            WalkthroughRow(
                file=file_,
                change=change,  # type: ignore[arg-type]
                summary=summary,
            )
        )
    return rows


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
