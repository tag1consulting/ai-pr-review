"""Pydantic Finding model — typed representation of a single review finding.

Closes #190 (schema validation) and #126 (to_finding() framework).
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["Critical", "High", "Medium", "Low"]

# Shared taxonomy ported from claude-comprehensive-review#76. Finding.category
# is typed `Category` below, but what actually prevents an unrecognized value
# from being rejected is the `mode="before"` validator (_normalise_category):
# it runs before pydantic's type check and coerces any unknown/missing input
# to "other" ahead of time, so the Literal type never sees an invalid value to
# reject. CATEGORIES is derived from Category via get_args() so there is
# exactly one place that lists the 11 values; the tuple form is what
# _normalise_category checks membership against.
Category = Literal[
    "authz",
    "injection",
    "dependency-cve",
    "secret",
    "architecture-coupling",
    "test-gap",
    "edge-case",
    "observability",
    "docs",
    "lint",
    "other",
]
CATEGORIES: tuple[str, ...] = get_args(Category)


class Finding(BaseModel):
    """A single finding from an agent or static analyzer."""

    severity: Severity
    confidence: int = Field(ge=0, le=100)
    finding: str = Field(min_length=1)
    source: str = ""
    category: Category = "other"
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    start_line: int | None = Field(default=None, ge=1)
    remediation: str = ""
    suggested_code: str = ""
    sources: list[str] = Field(default_factory=list)
    # cve-check only: "dependency-check" tag required for parity with bash output.
    agent: str = ""
    # Set by apply_diff_scope when a native-analyzer finding falls outside the
    # changed-line set.  Findings with out_of_diff=True are capped to Low
    # severity and rendered in a collapsed body section rather than the main
    # findings list.
    out_of_diff: bool = False
    # Set by merge._collapse_cluster when the proximity cluster contains BOTH
    # at least one native static-analyzer source AND at least one LLM-agent
    # source — independent corroboration of the same file+line region.
    # Internal-only: not serialised by to_dict().
    corroborated: bool = False

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.capitalize()
            if v not in ("Critical", "High", "Medium", "Low"):
                raise ValueError(f"Invalid severity {v!r}")
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _normalise_category(cls, v: object) -> object:
        # Permissive by design, unlike _normalise_severity: an LLM emitting an
        # unrecognized or missing category must never cause the finding to be
        # dropped. Falls back to "other" instead of raising.
        if isinstance(v, str):
            v = v.strip().lower()
            if v not in CATEGORIES:
                return "other"
            return v
        return "other"

    @model_validator(mode="after")
    def _populate_sources(self) -> Finding:
        if self.source and not self.sources:
            self.sources = [self.source]
        return self

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict matching the bash schema."""
        d: dict[str, object] = {
            "severity": self.severity,
            "confidence": self.confidence,
            "finding": self.finding,
            "source": self.source,
            "category": self.category,
        }
        if self.file:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        if self.start_line is not None:
            d["start_line"] = self.start_line
        if self.remediation:
            d["remediation"] = self.remediation
        if self.suggested_code:
            d["suggested_code"] = self.suggested_code
        if self.sources:
            d["sources"] = self.sources
        return d
