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
    sanitize_mermaid,
    sanitize_summary_markdown,
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
    assert result.walkthrough == ()


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
    # Prompt must not model the broken parenthesis-in-alias/label pattern.
    assert "method(args)" not in result
    # Prompt must instruct the model to avoid special chars in aliases.
    assert "parentheses" in result


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
        walkthrough=(),
        sequence_diagram=None,
    )
    with pytest.raises(FrozenInstanceError):
        result.effort = 5  # type: ignore[misc]


def test_walkthrough_row_is_frozen() -> None:
    row = WalkthroughRow(file="a.py", change="Added", summary="x")
    with pytest.raises(FrozenInstanceError):
        row.file = "b.py"  # type: ignore[misc]


def test_walkthrough_row_rejects_empty_file() -> None:
    with pytest.raises(ValueError, match="file must be non-empty"):
        WalkthroughRow(file="", change="Added", summary="x")


def test_walkthrough_row_rejects_empty_summary() -> None:
    with pytest.raises(ValueError, match="summary must be non-empty"):
        WalkthroughRow(file="a.py", change="Added", summary="")


def test_walkthrough_row_rejects_invalid_change() -> None:
    with pytest.raises(ValueError, match="change must be one of"):
        WalkthroughRow(file="a.py", change="Wibble", summary="x")  # type: ignore[arg-type]


def test_summarizer_output_rejects_invalid_pr_type() -> None:
    with pytest.raises(ValueError, match="pr_type must be one of"):
        SummarizerOutput(
            summary_md="",
            pr_type="wibble",  # type: ignore[arg-type]
            effort=2,
            walkthrough=(),
            sequence_diagram=None,
        )


def test_summarizer_output_rejects_out_of_range_effort() -> None:
    with pytest.raises(ValueError, match="effort must be in"):
        SummarizerOutput(
            summary_md="",
            pr_type="feature",
            effort=0,
            walkthrough=(),
            sequence_diagram=None,
        )


def test_parse_walkthrough_preserves_pipes_in_summary() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 2/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| src/re.py | Modified | Adds regex for foo | bar matching |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert len(result.walkthrough) == 1
    assert "foo | bar matching" in result.walkthrough[0].summary


def test_parse_walkthrough_handles_escaped_pipe() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 2/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| src/re.py | Modified | literal \\| pipe inside |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert len(result.walkthrough) == 1
    assert result.walkthrough[0].summary == "literal | pipe inside"


def test_parse_warnings_empty_on_happy_path() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 3/5 — medium

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | new file |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.parse_warnings == ()
    assert result.is_degraded is False


def test_parse_warnings_populated_on_missing_type() -> None:
    raw = """\
## Summary

x

**Effort:** 3/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert "missing-type-line" in result.parse_warnings
    assert result.is_degraded is True


def test_parse_warnings_populated_on_unknown_type() -> None:
    raw = """\
## Summary

x

**Type:** wibble
**Effort:** 3/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert "unknown-pr-type" in result.parse_warnings


def test_parse_warnings_populated_on_dropped_walkthrough_rows() -> None:
    raw = """\
## Summary

x

**Type:** feature
**Effort:** 3/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Wibble | bad change verb |
| b.py | Added | ok |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    # Static tag — callers can use `in result.parse_warnings` directly
    assert "walkthrough-rows-dropped" in result.parse_warnings


def test_parse_ignores_section_heading_inside_fenced_code() -> None:
    # A `## Heading` inside a fenced code block must NOT carve a new section
    raw = """\
## Summary

Example of a heading inside a fence:

```markdown
## This is not a real heading
```

**Type:** feature
**Effort:** 4/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | x |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.pr_type == "feature"
    assert result.effort == 4


def test_parse_fence_longer_than_three_ticks() -> None:
    # CommonMark: a fence opened with ````` (4 ticks) is NOT closed by ``` (3 ticks).
    # Previously the fence-matcher truncated openers to [:3], causing premature close.
    raw = """\
## Summary

Example showing an embedded fence:

````markdown
```python
## not a section header — indentation example
```
````

**Type:** refactor
**Effort:** 2/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | x |
"""
    result = parse_summarizer_output(raw, include_diagram=False)
    assert result.pr_type == "refactor"
    assert result.effort == 2


# ---------------------------------------------------------------------------
# sanitize_mermaid
# ---------------------------------------------------------------------------

def test_sanitize_mermaid_strips_parens_from_alias() -> None:
    block = """\
sequenceDiagram
    participant GH as GitHubProvider.post_review()
    participant Body as truncate_body()
    participant Marker as build_id_map_marker()
    GH->>Marker: build_id_map_marker(id_map)
    Marker-->>GH: id_map_marker (N bytes)"""
    result = sanitize_mermaid(block)
    assert "post_review()" not in result
    assert "truncate_body()" not in result
    assert "build_id_map_marker()" not in result
    # participant id and alias prefix are preserved
    assert "participant GH as GitHubProvider.post_review" in result
    assert "participant Body as truncate_body" in result
    # message labels are left intact (parens after : are tolerated by Mermaid)
    assert "GH->>Marker: build_id_map_marker(id_map)" in result


def test_sanitize_mermaid_leaves_clean_aliases_unchanged() -> None:
    block = """\
sequenceDiagram
    participant A as Caller
    participant B as Callee
    A->>B: callMethod
    B-->>A: result"""
    assert sanitize_mermaid(block) == block


def test_sanitize_mermaid_leaves_alt_blocks_unchanged() -> None:
    block = """\
sequenceDiagram
    participant GH as Provider
    alt marker_reserve > MAX - MIN_BODY_BYTES
        GH->>GH: log warning
    end
    GH->>GH: post review"""
    result = sanitize_mermaid(block)
    assert "alt marker_reserve > MAX - MIN_BODY_BYTES" in result
    assert "end" in result


def test_sanitize_mermaid_strips_brackets_and_angle_brackets_from_alias() -> None:
    block = """\
sequenceDiagram
    participant A as Foo[bar]
    participant B as Baz<T>
    A->>B: go
    B-->>A: done"""
    result = sanitize_mermaid(block)
    assert "[" not in result.split("A->>B")[0]  # alias portion only
    assert "<" not in result.split("A->>B")[0]


def test_sanitize_mermaid_handles_actor_keyword() -> None:
    block = """\
sequenceDiagram
    actor U as User()
    participant S as Server
    U->>S: request
    S-->>U: response"""
    result = sanitize_mermaid(block)
    assert "User()" not in result
    assert "actor U as User" in result


def test_sanitize_mermaid_is_valid_after_cleaning_pr367_diagram() -> None:
    """The exact broken diagram from PR #367 must be valid after sanitization."""
    block = """\
sequenceDiagram
    participant GH as GitHubProvider.post_review()
    participant Body as truncate_body()
    participant Marker as build_id_map_marker()

    GH->>Marker: build_id_map_marker(id_map)
    Marker-->>GH: id_map_marker (N bytes)

    alt marker_reserve > MAX - MIN_BODY_BYTES
        GH->>GH: log warning (owner/repo/PR#, marker size)
        GH->>GH: id_map_marker = ""; marker_reserve = 0
    end

    GH->>GH: truncate_limit = MAX_BODY_SIZE - marker_reserve
    GH->>Body: truncate_body(body, limit=truncate_limit)
    Body-->>GH: truncated body

    alt id_map_marker non-empty
        GH->>GH: body += "\\n" + id_map_marker
    end

    GH->>GH: post review body"""
    result = sanitize_mermaid(block)
    # No parens in participant alias lines
    for line in result.splitlines():
        if line.lstrip().startswith(("participant ", "actor ")):
            if " as " in line:
                alias_part = line.split(" as ", 1)[1]
                assert "(" not in alias_part, f"paren in alias: {line!r}"
                assert ")" not in alias_part, f"paren in alias: {line!r}"
    # Block is still valid Mermaid
    from ai_pr_review.agents.summarizer import is_valid_mermaid
    assert is_valid_mermaid(result)


# ---------------------------------------------------------------------------
# sanitize_summary_markdown
# ---------------------------------------------------------------------------

def test_sanitize_summary_markdown_fixes_mermaid_in_full_body() -> None:
    markdown = """\
## Summary

Refactors the auth flow.

**Type:** bugfix
**Effort:** 3/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| foo.py | Modified | Fix truncation |

## Sequence Diagrams

```mermaid
sequenceDiagram
    participant GH as GitHubProvider.post_review()
    participant B as truncate_body()
    GH->>B: truncate_body(text)
    B-->>GH: truncated
```
"""
    result = sanitize_summary_markdown(markdown)
    # Aliases sanitized
    assert "post_review()" not in result
    assert "truncate_body()" not in result
    # Walkthrough and summary sections are preserved verbatim
    assert "## Walkthrough" in result
    assert "Fix truncation" in result
    assert "## Summary" in result
    # The mermaid fence structure is preserved
    assert "```mermaid" in result
    assert "```" in result


def test_sanitize_summary_markdown_noop_when_no_mermaid() -> None:
    markdown = "## Summary\n\nNo diagram here.\n"
    assert sanitize_summary_markdown(markdown) == markdown


def test_sanitize_summary_markdown_noop_when_already_clean() -> None:
    markdown = """\
## Summary

Clean.

## Sequence Diagrams

```mermaid
sequenceDiagram
    participant A as Caller
    participant B as Callee
    A->>B: go
    B-->>A: done
```
"""
    # Should return the text structurally equivalent (modulo trailing newline in block)
    result = sanitize_summary_markdown(markdown)
    assert "participant A as Caller" in result
    assert "participant B as Callee" in result
