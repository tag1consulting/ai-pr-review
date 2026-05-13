"""Tests for ai_pr_review.agents.summarizer."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ai_pr_review.agents.summarizer import (
    SummarizerOutput,
    WalkthroughRow,
    build_summarizer_system_prompt,
    build_summarizer_user_message,
    is_valid_mermaid,
    parse_summarizer_output,
)

# ---------------------------------------------------------------------------
# is_valid_mermaid
# ---------------------------------------------------------------------------

def test_is_valid_mermaid_accepts_well_formed() -> None:
    block = """\
sequenceDiagram
    participant A
    participant B
    A->>B: hello
    B-->>A: reply
"""
    assert is_valid_mermaid(block) is True


def test_is_valid_mermaid_rejects_missing_header() -> None:
    block = "A->>B: hello\nB-->>A: reply"
    assert is_valid_mermaid(block) is False


def test_is_valid_mermaid_rejects_empty_body() -> None:
    assert is_valid_mermaid("sequenceDiagram\n") is False


def test_is_valid_mermaid_rejects_without_arrows() -> None:
    block = "sequenceDiagram\n    participant A\n    participant B\n"
    assert is_valid_mermaid(block) is False


def test_is_valid_mermaid_rejects_nested_code_fence() -> None:
    block = "sequenceDiagram\n    A->>B: hi\n```\n"
    assert is_valid_mermaid(block) is False


def test_is_valid_mermaid_accepts_leading_whitespace() -> None:
    block = "   \n\n  sequenceDiagram\n    A->>B: hi\n"
    assert is_valid_mermaid(block) is True


# ---------------------------------------------------------------------------
# parse_summarizer_output — happy path
# ---------------------------------------------------------------------------

def test_parse_happy_path_no_diagram() -> None:
    raw = """\
## Summary

This PR adds a new widget handler.

**Type:** feature
**Effort:** 3/5 — medium refactor

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| src/handler.py | Added | New widget handler |
| src/routes.py | Modified | Registers the handler |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert "adds a new widget handler" in result.summary_md
    assert result.pr_type == "feature"
    assert result.effort == 3
    assert len(result.walkthrough) == 2
    assert result.walkthrough[0] == WalkthroughRow(
        file="src/handler.py", change="Added", summary="New widget handler"
    )
    assert result.sequence_diagram is None


def test_parse_happy_path_with_diagram() -> None:
    raw = """\
## Summary

Refactors the auth flow.

**Type:** refactor
**Effort:** 4/5 — cross-cutting

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| auth.py | Modified | New flow |

## Sequence Diagrams

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: login
    S-->>C: token
```
"""
    result = parse_summarizer_output(raw, include_diagram=True)
    assert result.sequence_diagram is not None
    assert "sequenceDiagram" in result.sequence_diagram
    assert "C->>S: login" in result.sequence_diagram


def test_parse_drops_malformed_mermaid() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 2/5 — small

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | new file |

## Sequence Diagrams

```mermaid
This is not a valid sequence diagram.
```
"""
    result = parse_summarizer_output(raw, include_diagram=True)
    assert result.sequence_diagram is None


# ---------------------------------------------------------------------------
# parse_summarizer_output — fallbacks
# ---------------------------------------------------------------------------

def test_parse_missing_summary_heading_yields_empty_summary() -> None:
    raw = """\
## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | x |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.summary_md == ""


def test_parse_missing_walkthrough_yields_empty_list() -> None:
    raw = """\
## Summary

x

**Type:** docs
**Effort:** 1/5 — trivial
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.walkthrough == []


def test_parse_effort_out_of_range_defaults_to_3() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 99/5 — garbage

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.effort == 3


def test_parse_effort_unparseable_defaults_to_3() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** nonsense

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.effort == 3


def test_parse_unknown_pr_type_defaults_to_mixed() -> None:
    raw = """\
## Summary

x

**Type:** wibble
**Effort:** 2/5 — small

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.pr_type == "mixed"


def test_parse_pr_type_normalized_to_lowercase() -> None:
    raw = """\
## Summary

x

**Type:** Feature
**Effort:** 2/5 — small

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.pr_type == "feature"


def test_parse_walkthrough_skips_separator_and_section_rows() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 2/5 — small

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| **API layer** | | |
| src/api.py | Added | new endpoint |
| src/model.py | Modified | add field |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    # The **API layer** row is a section header with empty change/summary; skip it
    assert len(result.walkthrough) == 2
    assert result.walkthrough[0].file == "src/api.py"


# ---------------------------------------------------------------------------
# build_summarizer_system_prompt
# ---------------------------------------------------------------------------

def test_system_prompt_without_diagram(tmp_path: Path) -> None:
    base = tmp_path / "pr-summarizer.md"
    base.write_text("BASE PROMPT BODY")
    result = build_summarizer_system_prompt(base, include_diagram=False)
    assert result == "BASE PROMPT BODY"


def test_system_prompt_with_diagram_appends_addendum(tmp_path: Path) -> None:
    base = tmp_path / "pr-summarizer.md"
    base.write_text("BASE PROMPT BODY")
    result = build_summarizer_system_prompt(base, include_diagram=True)
    assert "BASE PROMPT BODY" in result
    assert "Sequence Diagram" in result
    assert "mermaid" in result


def test_system_prompt_missing_base_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    with pytest.raises(FileNotFoundError):
        build_summarizer_system_prompt(missing, include_diagram=False)


# ---------------------------------------------------------------------------
# build_summarizer_user_message
# ---------------------------------------------------------------------------

def test_user_message_formats_all_sections() -> None:
    msg = build_summarizer_user_message(
        manifest="FILES: 3",
        commit_log="abc123 commit message",
        diff_text="diff --git a/x b/x\n+new line",
    )
    assert "## Manifest" in msg
    assert "FILES: 3" in msg
    assert "## Commit log" in msg
    assert "abc123" in msg
    assert "## Diff" in msg
    assert "+new line" in msg


def test_user_message_handles_empty_inputs() -> None:
    msg = build_summarizer_user_message(manifest="", commit_log="", diff_text="")
    assert "## Manifest" in msg
    assert "## Commit log" in msg
    assert "## Diff" in msg


# ---------------------------------------------------------------------------
# SummarizerOutput dataclass is frozen
# ---------------------------------------------------------------------------

def test_summarizer_output_is_frozen() -> None:
    result = SummarizerOutput(
        summary_md="x",
        pr_type="feature",
        effort=2,
        walkthrough=[],
        sequence_diagram=None,
    )
    with pytest.raises(FrozenInstanceError):
        result.effort = 5  # type: ignore[misc]


def test_walkthrough_row_is_frozen() -> None:
    row = WalkthroughRow(file="a.py", change="Added", summary="x")
    with pytest.raises(FrozenInstanceError):
        row.file = "b.py"  # type: ignore[misc]
