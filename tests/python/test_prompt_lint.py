"""Prompt lint: keeps tool-runtime instructions and the NONE/[] contradiction
from re-entering prompts/*.md.

Background (ADR 0004, F4/F5): this engine's agents are each one tool-less
HTTP call whose output must survive `findings/extract.py`'s fenced-JSON
regex. Every agent prompt was, at various points, imported from
claude-comprehensive-review (Claude Code subagents with real Read/Bash/MCP
tool access, output read as free-form prose by an orchestrating skill).
That mismatch has already shipped a defect to a live PR comment (#446:
issue-linker instructed to run `gh issue view`/`git diff`, had no tools,
emitted literal `<tool_call>` XML) and produced a self-contradictory
empty-state instruction across the seven finding-agent prompts (#372 added
`NONE` to five of them while `_trailer-findings.md` — appended to all seven
— says emit `[]`, and `findings/extract.py` has no `NONE` handling at all:
an agent that obeys it fails the fence match and its entire review is
silently discarded).

This is a regression lint, not a general prompt-quality check: it enforces
the specific invariants those two incidents established, so the next prompt
edit that borrows one instruction from a tool-having plugin gets caught at
review time instead of in a posted PR comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Every prompt in this engine backs a tool-less HTTP call. None of these
# markers can ever be actionable, in any prompt file.
_TOOL_AFFORDANCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "Read tool": re.compile(r"\bRead tool\b"),
    "executable git diff invocation": re.compile(r"git diff\s*(--|<|@\{)"),
    "gh CLI invocation": re.compile(r"\bgh (issue|pr|api)\b"),
    "MCP tool reference": re.compile(r"\bmcp__"),
    "EXTENDED_THINKING flag": re.compile(r"\bEXTENDED_THINKING\b"),
}

# The seven agents whose output ai_pr_review/findings/extract.py parses as a
# fenced json-findings block. issue-linker.md and pr-summarizer.md are
# preflight prose agents parsed separately by review/preflight.py, which
# does understand a bare NONE — they are deliberately excluded below.
_FINDING_AGENT_PROMPTS = (
    "adversarial-general.md",
    "architecture-reviewer.md",
    "blind-hunter.md",
    "code-reviewer.md",
    "edge-case-hunter.md",
    "security-reviewer.md",
    "silent-failure-hunter.md",
)

# The exact phrasing #372's backport used to instruct emitting the bare word
# NONE. Every finding-agent prompt now instead says "Do NOT output the bare
# word `NONE`" (matching _trailer-findings.md's `[]` empty state), so this
# phrase should never reappear in any of them.
_POSITIVE_NONE_INSTRUCTION = re.compile(r"output exactly", re.IGNORECASE)


def _all_prompt_files() -> list[Path]:
    files = sorted(PROMPTS_DIR.glob("*.md"))
    assert files, f"expected prompt files under {PROMPTS_DIR}"
    return files


@pytest.mark.parametrize("prompt_path", _all_prompt_files(), ids=lambda p: p.name)
def test_no_tool_affordance_markers(prompt_path: Path) -> None:
    content = prompt_path.read_text()
    for label, pattern in _TOOL_AFFORDANCE_PATTERNS.items():
        match = pattern.search(content)
        assert match is None, (
            f"{prompt_path.name} contains a {label} ({match.group(0)!r}), but no "
            "agent in this engine has tool access — see ADR 0004"
        )


@pytest.mark.parametrize("filename", _FINDING_AGENT_PROMPTS)
def test_no_bare_none_instruction(filename: str) -> None:
    content = (PROMPTS_DIR / filename).read_text()
    match = _POSITIVE_NONE_INSTRUCTION.search(content)
    assert match is None, (
        f"{filename} instructs outputting a bare sentinel word (matched "
        f"{match.group(0)!r}), but findings/extract.py has no handling for "
        "anything but a fenced json-findings block — an agent that obeys "
        "this has its entire review silently discarded (#372, F5)"
    )
