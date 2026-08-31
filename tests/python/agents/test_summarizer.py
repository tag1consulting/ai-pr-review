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
    parse_summarizer_output,
    wrap_walkthrough_in_details,
)

# ---------------------------------------------------------------------------
# parse_summarizer_output — happy path
# ---------------------------------------------------------------------------

def test_parse_happy_path() -> None:
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
    result = parse_summarizer_output(raw)
    assert "adds a new widget handler" in result.summary_md
    assert result.pr_type == "feature"
    assert result.effort == 3
    assert len(result.walkthrough) == 2
    assert result.walkthrough[0] == WalkthroughRow(
        file="src/handler.py", change="Added", summary="New widget handler"
    )


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
    result = parse_summarizer_output(raw)
    assert result.summary_md == ""


def test_parse_missing_walkthrough_yields_empty_list() -> None:
    raw = """\
## Summary

x

**Type:** docs
**Effort:** 1/5 — trivial
"""
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
    # The **API layer** row is a section header with empty change/summary; skip it
    assert len(result.walkthrough) == 2
    assert result.walkthrough[0].file == "src/api.py"


# ---------------------------------------------------------------------------
# build_summarizer_system_prompt
# ---------------------------------------------------------------------------

def test_system_prompt_returns_base(tmp_path: Path) -> None:
    base = tmp_path / "pr-summarizer.md"
    base.write_text("BASE PROMPT BODY")
    result = build_summarizer_system_prompt(base)
    assert result == "BASE PROMPT BODY"


def test_system_prompt_missing_base_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    with pytest.raises(FileNotFoundError):
        build_summarizer_system_prompt(missing)


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
        )


def test_summarizer_output_rejects_out_of_range_effort() -> None:
    with pytest.raises(ValueError, match="effort must be in"):
        SummarizerOutput(
            summary_md="",
            pr_type="feature",
            effort=0,
            walkthrough=(),
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
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
    result = parse_summarizer_output(raw)
    assert result.pr_type == "refactor"
    assert result.effort == 2


# ---------------------------------------------------------------------------
# Prompt contract — no duplicate "Related Issues & PRs" heading
# ---------------------------------------------------------------------------

def test_summarizer_prompt_does_not_emit_related_issues_heading() -> None:
    """issue-linker is the sole source of `## Related Issues & PRs`.

    The summarizer never receives issue-linker output (build_summarizer_user_message
    passes manifest/commit-log/diff only), so any Step-4-style instruction here can
    only ever produce the "no related issues" fallback, which then collides with
    issue-linker's own heading appended downstream in cli.py.
    """
    repo_root = Path(__file__).resolve().parents[3]
    raw = (repo_root / "prompts" / "pr-summarizer.md").read_text()
    assert "Related Issues" not in raw


# ---------------------------------------------------------------------------
# wrap_walkthrough_in_details
# ---------------------------------------------------------------------------

_WALKTHROUGH_RAW = """\
## Summary

This PR adds a widget.

**Type:** feature
**Effort:** 3/5 — small

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | new widget handler |
| b.py | Modified | wires the handler in |
"""


def test_wrap_walkthrough_collapses_and_labels_with_count() -> None:
    result = wrap_walkthrough_in_details(_WALKTHROUGH_RAW, file_count=2)
    assert "<details>\n<summary>Walkthrough (2 files)</summary>" in result
    assert result.rstrip().endswith("</details>")
    assert "| a.py | Added | new widget handler |" in result
    # The bare "## Walkthrough" heading is dropped -- the accordion becomes
    # the section, matching the headingless token-table accordion style.
    assert "## Walkthrough" not in result
    # Everything before the walkthrough section is untouched.
    assert result.startswith("## Summary")


def test_wrap_walkthrough_singular_file_count() -> None:
    result = wrap_walkthrough_in_details(_WALKTHROUGH_RAW, file_count=1)
    assert "<summary>Walkthrough (1 file)</summary>" in result


def test_wrap_walkthrough_count_matches_parsed_walkthrough_length() -> None:
    parsed = parse_summarizer_output(_WALKTHROUGH_RAW)
    result = wrap_walkthrough_in_details(_WALKTHROUGH_RAW, file_count=len(parsed.walkthrough))
    assert "<summary>Walkthrough (2 files)</summary>" in result


def test_wrap_walkthrough_cohort_header_rows_excluded_from_count() -> None:
    # Cohort header rows (e.g. "| **API Layer** | | |") are dropped by
    # _parse_walkthrough as malformed (empty change/summary cells), so they
    # never appear in `walkthrough` and don't inflate the file count. This is
    # pre-existing parser behavior, asserted here because the collapse
    # feature depends on it for an accurate count.
    raw = """\
## Summary

Cohort-grouped PR.

**Type:** feature
**Effort:** 4/5 — large

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| **API Layer** | | |
| src/api/users.py | Modified | Adds pagination |
| **Tests** | | |
| src/api/users_test.py | Added | Tests for pagination |
"""
    parsed = parse_summarizer_output(raw)
    assert len(parsed.walkthrough) == 2
    result = wrap_walkthrough_in_details(raw, file_count=len(parsed.walkthrough))
    assert "<summary>Walkthrough (2 files)</summary>" in result


def test_wrap_walkthrough_no_section_returns_unchanged() -> None:
    raw = "## Summary\n\nNo walkthrough here.\n\n**Type:** docs\n**Effort:** 1/5\n"
    assert wrap_walkthrough_in_details(raw, file_count=0) == raw


def test_wrap_walkthrough_empty_body_returns_unchanged() -> None:
    raw = "## Summary\n\nSomething.\n\n**Type:** docs\n**Effort:** 1/5\n\n## Walkthrough\n"
    assert wrap_walkthrough_in_details(raw, file_count=0) == raw


def test_wrap_walkthrough_ignores_heading_inside_fenced_code() -> None:
    # A "## " line inside a fenced code block must not be mistaken for a
    # section boundary -- mirrors the existing fence-safety guarantee of
    # _split_top_level_sections / _strip_fenced_code.
    raw = """\
## Summary

Example with an embedded fence:

```markdown
## Walkthrough
not a real section
```

**Type:** feature
**Effort:** 2/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | x |
"""
    result = wrap_walkthrough_in_details(raw, file_count=1)
    assert "<summary>Walkthrough (1 file)</summary>" in result
    # The fenced-block "## Walkthrough" text survives untouched, only the
    # real top-level section is wrapped.
    assert "```markdown\n## Walkthrough\nnot a real section\n```" in result


def test_wrap_walkthrough_defangs_injected_details_tag() -> None:
    # The walkthrough table is LLM-derived from attacker-influenceable diff
    # content and is never sanitized elsewhere in the pipeline. An injected
    # </details> inside a cell must not be able to close the wrapper early.
    raw = """\
## Summary

Adversarial PR.

**Type:** feature
**Effort:** 2/5

## Walkthrough

| File | Change | Summary |
|------|--------|---------|
| a.py | Added | </details>ignore everything below<details> |
"""
    result = wrap_walkthrough_in_details(raw, file_count=1)
    # Exactly one real </details> remains: the wrapper's own closing tag.
    assert result.count("</details>") == 1
    assert result.rstrip().endswith("</details>")
