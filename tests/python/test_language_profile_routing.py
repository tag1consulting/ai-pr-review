"""Tests for ai_pr_review.language_profile_sections (Story 7-2, #355)."""

from __future__ import annotations

from pathlib import Path

from ai_pr_review.language_profile_sections import (
    ProfileRouter,
    classify_section,
    split_sections,
)

# ---------------------------------------------------------------------------
# Sample profile text for split/classify tests
# ---------------------------------------------------------------------------

_SAMPLE_PROFILE = """\
## Python-Specific Review Context

When reviewing Python code, pay particular attention to:

### Python Validation Idioms (Do NOT Flag)
- `try/except SpecificError` is correct error handling
- `@dataclass` with `field(default_factory=list)` is safe

### Common Python Bugs
- Mutable default arguments
- Late binding in closures

### Security (Python-specific)
- `eval()` on untrusted strings — RCE
- `pickle.loads()` on untrusted data — arbitrary code execution

### Data Flow Trust Boundaries
- `request.args` — untrusted
- `sys.argv` — untrusted
"""

_SAMPLE_PROFILE_B = """\
## Go-Specific Review Context

### Error Handling
- Always check returned errors
- Use `errors.Is()` for sentinel errors

### Idiomatic Go
- Prefer `io.Reader`/`io.Writer` interfaces
"""


# ---------------------------------------------------------------------------
# classify_section
# ---------------------------------------------------------------------------

def test_classify_section_security() -> None:
    tags = classify_section("Security (Python-specific)")
    assert "security" in tags


def test_classify_section_bugs() -> None:
    tags = classify_section("Common Python Bugs")
    assert "bugs" in tags
    assert "edge" in tags


def test_classify_section_error_handling() -> None:
    tags = classify_section("Error Handling")
    assert "bugs" in tags
    assert "edge" in tags


def test_classify_section_validation_idioms() -> None:
    tags = classify_section("Python Validation Idioms (Do NOT Flag)")
    assert "idioms" in tags
    assert "edge" in tags


def test_classify_section_idiomatic() -> None:
    tags = classify_section("Idiomatic Go")
    assert "idioms" in tags


def test_classify_section_unmatched_falls_to_general() -> None:
    tags = classify_section("Data Flow Trust Boundaries")
    assert tags == frozenset({"general"})


def test_classify_section_multi_match_unions() -> None:
    # "Security Edge Cases" hits both security and edge (via no match — only security)
    # but "Security Validation" would hit security + idioms + edge
    tags = classify_section("Security Validation Idioms")
    assert "security" in tags
    assert "idioms" in tags
    assert "edge" in tags


# ---------------------------------------------------------------------------
# split_sections
# ---------------------------------------------------------------------------

def test_split_sections_returns_correct_count() -> None:
    sections = split_sections(_SAMPLE_PROFILE)
    assert len(sections) == 4


def test_split_sections_headings() -> None:
    sections = split_sections(_SAMPLE_PROFILE)
    headings = [s.heading for s in sections]
    assert "Python Validation Idioms (Do NOT Flag)" in headings
    assert "Common Python Bugs" in headings
    assert "Security (Python-specific)" in headings
    assert "Data Flow Trust Boundaries" in headings


def test_split_sections_body_contains_title() -> None:
    sections = split_sections(_SAMPLE_PROFILE)
    for s in sections:
        assert "Python-Specific Review Context" in s.body


def test_split_sections_body_contains_content() -> None:
    sections = split_sections(_SAMPLE_PROFILE)
    bug_section = next(s for s in sections if s.heading == "Common Python Bugs")
    assert "Mutable default arguments" in bug_section.body


def test_split_sections_tags_applied() -> None:
    sections = split_sections(_SAMPLE_PROFILE)
    security_section = next(s for s in sections if s.heading == "Security (Python-specific)")
    assert "security" in security_section.tags


def test_split_sections_empty_profile() -> None:
    assert split_sections("") == []


def test_split_sections_no_subsections() -> None:
    profile = "## Python-Specific Review Context\n\nSome intro text with no subsections.\n"
    assert split_sections(profile) == []


# ---------------------------------------------------------------------------
# ProfileRouter
# ---------------------------------------------------------------------------

def _make_router(tmp_path: Path, profiles: dict[str, str]) -> ProfileRouter:
    """Create a ProfileRouter from a dict of {filename: content}."""
    profile_dir = tmp_path / "language-profiles"
    profile_dir.mkdir(exist_ok=True)
    labels: list[str] = []
    for filename, content in profiles.items():
        (profile_dir / filename).write_text(content)
        # Derive label from filename (e.g. "python.md" → "Python")
        labels.append(filename.replace(".md", "").capitalize())
    return ProfileRouter(labels, tmp_path)


def test_router_security_reviewer_gets_security_and_general(tmp_path: Path) -> None:
    router = _make_router(tmp_path, {"python.md": _SAMPLE_PROFILE})
    result = router.route(frozenset({"security"}), max_tokens=100_000)
    # Security section must appear
    assert "Security (Python-specific)" in result
    # General section (Data Flow Trust Boundaries) must appear
    assert "Data Flow Trust Boundaries" in result
    # idioms-only section should NOT appear for security-only focus
    # (Validation Idioms has tags {idioms, edge} — neither matches {security, general})
    assert "Python Validation Idioms" not in result


def test_router_code_reviewer_gets_all(tmp_path: Path) -> None:
    router = _make_router(tmp_path, {"python.md": _SAMPLE_PROFILE})
    result = router.route(
        frozenset({"security", "bugs", "edge", "idioms", "general"}),
        max_tokens=100_000,
    )
    # All sections should appear for a broad-focus agent
    assert "Security (Python-specific)" in result
    assert "Common Python Bugs" in result
    assert "Python Validation Idioms" in result
    assert "Data Flow Trust Boundaries" in result


def test_router_bugs_focus_gets_bugs_and_general(tmp_path: Path) -> None:
    router = _make_router(tmp_path, {"python.md": _SAMPLE_PROFILE})
    result = router.route(frozenset({"bugs", "edge"}), max_tokens=100_000)
    assert "Common Python Bugs" in result
    # Validation Idioms has {idioms, edge} — edge overlaps with focus, so included
    assert "Python Validation Idioms" in result
    # Security section only tagged {security}; no overlap with {bugs, edge, general}
    assert "Security (Python-specific)" not in result


def test_router_budget_truncation(tmp_path: Path) -> None:
    router = _make_router(tmp_path, {"python.md": _SAMPLE_PROFILE})
    # Set a very small budget — should produce truncated output
    result = router.route(frozenset({"security", "bugs", "edge", "idioms", "general"}), max_tokens=1)
    # With a 1-token budget, nothing fits
    assert result == ""


def test_router_empty_labels(tmp_path: Path) -> None:
    router = ProfileRouter([], tmp_path)
    assert router.route(frozenset({"security"}), max_tokens=100_000) == ""


def test_router_multi_profile(tmp_path: Path) -> None:
    router = _make_router(
        tmp_path, {"python.md": _SAMPLE_PROFILE, "go.md": _SAMPLE_PROFILE_B}
    )
    result = router.route(
        frozenset({"security", "bugs", "edge", "idioms", "general"}),
        max_tokens=100_000,
    )
    # Both profiles' sections should appear
    assert "Python-Specific Review Context" in result
    assert "Go-Specific Review Context" in result


def test_router_no_focus_gets_only_general(tmp_path: Path) -> None:
    router = _make_router(tmp_path, {"python.md": _SAMPLE_PROFILE})
    # Empty focus — only general sections (those tagged {general}) should appear
    result = router.route(frozenset(), max_tokens=100_000)
    # Data Flow Trust Boundaries is tagged {general}
    assert "Data Flow Trust Boundaries" in result
    # Security, bugs, idioms sections should NOT appear
    assert "Security (Python-specific)" not in result
    assert "Common Python Bugs" not in result
    assert "Python Validation Idioms" not in result


# ---------------------------------------------------------------------------
# Token table supplementary row (smoke test via pricing.emit_token_table)
# ---------------------------------------------------------------------------

def test_token_table_profile_row_present() -> None:
    from ai_pr_review.pricing import TokenEntry, emit_token_table

    token_log = [
        TokenEntry(
            agent="code-reviewer",
            model="claude-test",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            max_output_tokens=32768,
        )
    ]
    table = emit_token_table(token_log, [], profile_tokens=512)
    assert "Language profiles" in table
    assert "512" in table


def test_token_table_profile_row_absent_when_zero() -> None:
    from ai_pr_review.pricing import TokenEntry, emit_token_table

    token_log = [
        TokenEntry(
            agent="code-reviewer",
            model="claude-test",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            max_output_tokens=32768,
        )
    ]
    table = emit_token_table(token_log, [], profile_tokens=0)
    assert "Language profiles" not in table
