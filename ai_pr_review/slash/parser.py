"""Slash command parser — E3.S7.

Parses ``/ai-pr-review <command> [reason]`` comment bodies into typed
``SlashCommand`` objects.  The parser is deliberately narrow:

- Only the first non-empty line is inspected.
- Only ``/ai-pr-review`` prefix is recognized.
- The ``reason`` argument is sanitized: length capped at 1024 chars,
  control characters stripped, HTML-escaped, newlines replaced with spaces.

Supported commands:
  false-positive [reason]   — mark finding as false positive; store feedback
  wont-fix [reason]         — mark finding as intentional; store feedback
  explain                   — re-invoke originating agent with detailed explanation
  revise <hint>             — re-invoke agent with a revision hint
  feedback <text>           — store free-form feedback
  dismiss [reason]          — alias for false-positive (backward compat)

The ``author_association`` guard (OWNER/MEMBER/COLLABORATOR) is enforced at
the GitHub Actions workflow level before this parser is called; the parser
trusts the caller's pre-filtering.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

_PREFIX = "/ai-pr-review"
_MAX_REASON_LEN = 1024

# Known command names
KNOWN_COMMANDS: frozenset[str] = frozenset(
    {
        "false-positive",
        "wont-fix",
        "explain",
        "revise",
        "feedback",
        "dismiss",  # alias for false-positive
    }
)

# Secret patterns to reject from reason text (basic; not a full scan)
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token|auth)[=:]\S+"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36,}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9-]+"),
]


@dataclass(frozen=True)
class SlashCommand:
    """A parsed and sanitized slash command."""

    name: str
    reason: str  # sanitized
    raw_body: str

    @property
    def canonical_name(self) -> str:
        """Normalize 'dismiss' alias to 'false-positive'."""
        return "false-positive" if self.name == "dismiss" else self.name

    @property
    def is_feedback_command(self) -> bool:
        """True for commands that write to the feedback store."""
        return self.canonical_name in ("false-positive", "wont-fix", "feedback")


@dataclass
class ParseError:
    """Returned instead of SlashCommand when parsing fails."""

    message: str
    raw_body: str = field(default="")


def _sanitize_reason(raw: str) -> str:
    """Sanitize user-supplied reason text.

    - Strip leading/trailing whitespace
    - Replace control characters (except tab) with space
    - Collapse newlines to single spaces
    - Cap at MAX_REASON_LEN characters
    - HTML-escape to prevent delimiter escape in <repo-feedback> blocks
    - Reject if it matches known secret patterns (returns empty string)
    """
    # Normalize unicode to NFC first
    raw = unicodedata.normalize("NFC", raw)

    # Replace control chars (keep printable + space + tab)
    cleaned = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf") and ch not in ("\t",) else ch
        for ch in raw
    )
    # Collapse whitespace runs (including newlines normalized above)
    cleaned = " ".join(cleaned.split())

    # Cap length
    if len(cleaned) > _MAX_REASON_LEN:
        cleaned = cleaned[:_MAX_REASON_LEN]

    # Reject likely secrets
    for pattern in _SECRET_PATTERNS:
        if pattern.search(cleaned):
            return ""

    # HTML-escape so reason cannot break out of <repo-feedback> XML block
    return html.escape(cleaned, quote=True)


def parse_command(body: str) -> SlashCommand | ParseError | None:
    """Parse a comment body into a SlashCommand.

    Returns:
        SlashCommand — if the body starts with a recognized command
        ParseError   — if the prefix matches but the command is unknown/malformed
        None         — if the body is not a slash command at all
    """
    if not body:
        return None

    first_line = body.splitlines()[0].strip()
    if not first_line.startswith(_PREFIX):
        return None

    # Split prefix + rest
    rest = first_line[len(_PREFIX):].strip()
    if not rest:
        # Bare "/ai-pr-review" with nothing after — not a slash command
        return None

    parts = rest.split(None, 1)
    command = parts[0].lower()
    raw_reason = parts[1] if len(parts) > 1 else ""

    if command not in KNOWN_COMMANDS:
        return ParseError(
            message=f"Unknown command {command!r}. Known: {sorted(KNOWN_COMMANDS)}",
            raw_body=body,
        )

    reason = _sanitize_reason(raw_reason)

    return SlashCommand(name=command, reason=reason, raw_body=body)
