"""Feedback entry model — E3.S8.

A ``FeedbackEntry`` captures one piece of human feedback on an AI finding.
Stored as a single JSONL line in the GitBranchStore.

Fields are kept deliberately minimal so the schema can evolve forward-compatibly.
Unknown keys in JSON are ignored on load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FeedbackEntry:
    """One piece of human feedback on a finding."""

    # ISO-8601 UTC timestamp (set by store on write)
    ts: str

    # Slash command canonical name: "false-positive" | "wont-fix" | "feedback"
    command: str

    # User-supplied reason (already sanitized by parser.py)
    reason: str

    # Finding source tag (e.g. "code-reviewer", "sarif:bandit")
    source: str

    # File path the finding was on (may be empty for general feedback)
    file: str = ""

    # Rule / finding ID if available (from SARIF ruleId or structured text)
    rule_id: str = ""

    # Free-form extras for forward compatibility
    extras: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------------------
    # Serialization
    # ---------------------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "command": self.command,
                "reason": self.reason,
                "source": self.source,
                "file": self.file,
                "rule_id": self.rule_id,
                **self.extras,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> FeedbackEntry | None:
        """Parse one JSONL line; return None if malformed."""
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict):
            return None
        known = {"ts", "command", "reason", "source", "file", "rule_id"}
        extras = {k: v for k, v in d.items() if k not in known}
        return cls(
            ts=str(d.get("ts", "")),
            command=str(d.get("command", "")),
            reason=str(d.get("reason", "")),
            source=str(d.get("source", "")),
            file=str(d.get("file", "")),
            rule_id=str(d.get("rule_id", "")),
            extras=extras,
        )
