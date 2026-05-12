"""Pydantic Finding model — typed representation of a single review finding.

Closes #190 (schema validation) and #126 (to_finding() framework).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["Critical", "High", "Medium", "Low"]


class Finding(BaseModel):
    """A single finding from an agent or static analyzer."""

    severity: Severity
    confidence: int = Field(ge=0, le=100)
    finding: str = Field(min_length=1)
    source: str = ""
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    start_line: int | None = Field(default=None, ge=1)
    remediation: str = ""
    suggested_code: str = ""
    sources: list[str] = Field(default_factory=list)

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.capitalize()
            if v not in ("Critical", "High", "Medium", "Low"):
                raise ValueError(f"Invalid severity {v!r}")
        return v

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
