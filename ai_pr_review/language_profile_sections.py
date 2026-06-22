"""Per-agent language-profile section routing — Story 7-2 (#355).

Parses language-profile markdown files into sections by ``### `` heading
boundaries, classifies each section into focus tags, and assembles a
per-agent profile text containing only the sections relevant to that
agent's ``profile_focus``.

Focus tags:
    security  — sections about security vulnerabilities and controls
    bugs      — sections about common bugs and error-prone patterns
    edge      — sections about edge cases, validation, and boundary conditions
    idioms    — sections about idiomatic patterns the reviewer should NOT flag
    general   — sections that apply to every agent (always included)

Sections tagged ``general`` always reach every eligible agent; all other
tags are routed only to agents whose ``profile_focus`` includes that tag.
Headings may match multiple patterns; in that case the union of all
matching tag sets is used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ai_pr_review.context.budget import estimate_tokens
from ai_pr_review.language_profiles import load_language_profiles

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileSection:
    """A single ``### ``-delimited section from a language profile file."""

    heading: str
    body: str
    tags: frozenset[str]


def classify_section(heading: str) -> frozenset[str]:
    """Map a section heading to one or more focus tags by keyword matching.

    Matching is case-insensitive and substring-based to handle the natural
    variation in headings across 19 language profiles.  When a heading
    matches multiple patterns the results are unioned.
    """
    lower = heading.lower()
    tags: set[str] = set()

    if "security" in lower:
        tags.add("security")
    if "bug" in lower or "error handling" in lower:
        tags |= {"bugs", "edge"}
    if "validation" in lower or "do not flag" in lower:
        tags |= {"idioms", "edge"}
    if "idiomatic" in lower:
        tags.add("idioms")

    return frozenset(tags) if tags else frozenset({"general"})


def split_sections(profile_text: str) -> list[ProfileSection]:
    """Parse a profile into ``ProfileSection`` objects split by ``### `` boundaries.

    The ``## `` title line is treated as a shared header and prepended to
    every section body so each section is self-contained when rendered.
    """
    sections: list[ProfileSection] = []
    title_line = ""
    current_heading = ""
    current_body_lines: list[str] = []

    for line in profile_text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            title_line = line
        elif line.startswith("### "):
            stripped_heading = line[4:].strip()
            if current_heading:
                body = "\n".join(current_body_lines).strip()
                full_body = f"{title_line}\n\n### {current_heading}\n{body}" if title_line else f"### {current_heading}\n{body}"
                sections.append(
                    ProfileSection(
                        heading=current_heading,
                        body=full_body,
                        tags=classify_section(current_heading),
                    )
                )
            current_heading = stripped_heading
            current_body_lines = []
        elif current_heading:
            current_body_lines.append(line)

    if current_heading:
        body = "\n".join(current_body_lines).strip()
        full_body = f"{title_line}\n\n### {current_heading}\n{body}" if title_line else f"### {current_heading}\n{body}"
        sections.append(
            ProfileSection(
                heading=current_heading,
                body=full_body,
                tags=classify_section(current_heading),
            )
        )

    return sections


class ProfileRouter:
    """Parses all detected language profiles into classified sections once per run.

    Built in ``build_review_runtime`` from the detected language labels.
    ``route()`` assembles a per-agent profile text from sections matching
    that agent's focus tags, greedy-packed under a token budget.
    """

    def __init__(self, labels: list[str], script_dir: Path) -> None:
        raw_text = load_language_profiles(labels, script_dir)
        self._sections: list[ProfileSection] = []
        if raw_text:
            # load_language_profiles concatenates multiple profiles with
            # blank-line separators.  Split them apart by ## headers so
            # split_sections processes one profile at a time.
            self._sections = _split_all_profiles(raw_text)
            if not self._sections:
                logger.warning(
                    "ProfileRouter: profiles loaded but produced no parseable sections (labels=%s)",
                    labels,
                )

    def route(self, focus: frozenset[str], max_tokens: int) -> str:
        """Return assembled profile text for *focus*, packed under *max_tokens*.

        Sections whose tags overlap with ``focus | {"general"}`` are
        included; sections with no overlap are skipped.  Sections are
        packed greedily in document order until the token budget is
        exhausted.  Returns an empty string when there are no sections or
        all sections overflow the budget.
        """
        eligible_focus = focus | frozenset({"general"})
        parts: list[str] = []
        tokens_used = 0

        for section in self._sections:
            if not (section.tags & eligible_focus):
                continue
            section_tokens = estimate_tokens(section.body)
            if tokens_used + section_tokens > max_tokens:
                continue
            parts.append(section.body)
            tokens_used += section_tokens

        return "\n\n".join(parts)

    @property
    def total_tokens(self) -> int:
        """Estimated total tokens across all sections (pre-routing)."""
        return sum(estimate_tokens(s.body) for s in self._sections)


def _split_all_profiles(concatenated: str) -> list[ProfileSection]:
    """Split a concatenated multi-profile text into ``ProfileSection`` objects.

    ``load_language_profiles`` joins multiple profiles with ``\\n\\n``.
    We detect ``## `` lines as profile boundaries and process each profile
    independently so sections from different languages are not merged.
    """
    all_sections: list[ProfileSection] = []
    current_profile_lines: list[str] = []

    for line in concatenated.splitlines():
        if line.startswith("## ") and not line.startswith("### ") and current_profile_lines:
            all_sections.extend(split_sections("\n".join(current_profile_lines)))
            current_profile_lines = [line]
        else:
            current_profile_lines.append(line)

    if current_profile_lines:
        all_sections.extend(split_sections("\n".join(current_profile_lines)))

    return all_sections
