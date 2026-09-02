"""Slash command parser — E3.S7.

Parses ``/ai-pr-review <command> [reason]`` comment bodies into typed
``SlashCommand`` objects.  The parser is deliberately narrow:

- Only the first non-empty line is inspected.
- Only ``/ai-pr-review`` prefix is recognized.
- The ``reason`` argument is sanitized: length capped at 1024 chars,
  control characters stripped, HTML-escaped, newlines replaced with spaces.

Supported commands:
  false-positive [reason]         — mark finding as false positive; store feedback
  wont-fix [reason]               — mark finding as intentional; store feedback
  explain                         — re-invoke originating agent with detailed explanation
  revise <hint>                   — re-invoke agent with a revision hint
  feedback <text>                 — store free-form feedback
  dismiss [F<n>] [reason]         — alias for false-positive (backward compat); F<n> targets body-level finding
  explain [F<n>]                  — re-invoke originating agent for explanation; F<n> targets body-level finding
  revise [F<n>] <hint>            — re-invoke agent with revision hint; F<n> targets body-level finding
  fixed [F<n>] [sha] [reason]     — mark finding as fixed (not a suppression verdict; does NOT store feedback);
                                     optional commit SHA is echoed in the reply, not validated

``F<n>`` tokens also accept the bracketed form ``[F<n>]`` shown in review
bodies (e.g. ``**[F1]**``) — see ``_FID_RE``.

``parse_command`` only ever looks at the first line of the body it is given.
``parse_commands`` (plural) scans every line for the ``/ai-pr-review`` prefix
and parses each one independently, for callers that need to act on more than
one command posted in a single comment (issue #733).

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
        "fixed",  # NOT an alias — see SlashCommand.is_feedback_command
    }
)

# Matches a bare commit SHA (short or full, lowercase or upper). Same shape
# as vcs.marker's _SHA_PATTERN, duplicated here rather than imported to keep
# the parser free of a dependency on the vcs package.
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")

# Matches an F<n> finding-ID token, with optional surrounding square
# brackets. Findings are rendered in review bodies and inline comments as
# **[F1]** (see vcs/_finding_ids.py), so a user copying that token verbatim
# types "[F1]" rather than "F1" — accept both rather than silently
# mis-parsing the bracketed form as free-text reason (issue #735).
_FID_RE = re.compile(r"^\[?[Ff](\d+)\]?$")

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
    # For "dismiss F<n>" — the numeric body-finding ID (e.g. 1 for [F1]).
    # None when no ID was supplied (inline dismiss) or command is not dismiss.
    finding_id: int | None = None
    # For "fixed [F<n>] <sha>" — the commit SHA, lowercased. Empty string
    # when none was supplied or the command is not "fixed". Echoed in the
    # reply for audit-trail purposes only; never validated against the repo
    # (see slash/dismiss.py) and never used to drive thread resolution.
    commit_sha: str = ""

    @property
    def canonical_name(self) -> str:
        """Normalize 'dismiss' alias to 'false-positive'.

        "fixed" is deliberately NOT normalized to any existing command: it
        is neither an alias nor a feedback-store verdict (see
        is_feedback_command below).
        """
        return "false-positive" if self.name == "dismiss" else self.name

    @property
    def is_feedback_command(self) -> bool:
        """True for commands that write to the feedback store.

        "fixed" is intentionally excluded. The governance prompt
        (prompts/_governance.md) tells the model how to interpret exactly
        three verdict types (false-positive, wont-fix, feedback); an entry
        with command="fixed" would reach the model with no interpretive
        rule, and could be misread as a suppression signal for a finding
        that was actually correct. "fixed" rides the dismiss/resolve path
        only (ai_pr_review/slash/dismiss.py) and never touches the
        learning-loop store.
        """
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

    # For feedback/action commands — extract optional F<n> finding ID so
    # body-level findings can be acted on from a top-level PR comment.
    # Applies to: dismiss, false-positive, wont-fix, explain, revise, fixed.
    finding_id: int | None = None
    if (
        command in ("dismiss", "false-positive", "wont-fix", "explain", "revise", "fixed")
        and raw_reason
    ):
        # The first word may be a finding ID like "F3", "f3", or "[F3]"
        # (the bracketed form shown in review bodies — see _FID_RE above).
        id_parts = raw_reason.split(None, 1)
        fid_match = _FID_RE.match(id_parts[0])
        if fid_match:
            finding_id = int(fid_match.group(1))
            raw_reason = id_parts[1] if len(id_parts) > 1 else ""

    # "fixed" additionally accepts an optional commit SHA as the next
    # positional token, e.g. "/ai-pr-review fixed F3 abc1234 <reason>".
    # Peeled after the F-ID so the two optional tokens compose the same way
    # F-ID peeling already works for every other command. A token that
    # doesn't look like a SHA (e.g. ordinary reason prose) falls through to
    # `reason` unchanged — see the module docstring for the accepted
    # ambiguity when a prose word happens to be 7-40 hex characters.
    commit_sha = ""
    if command == "fixed" and raw_reason:
        sha_parts = raw_reason.split(None, 1)
        if _SHA_RE.fullmatch(sha_parts[0]):
            commit_sha = sha_parts[0].lower()
            raw_reason = sha_parts[1] if len(sha_parts) > 1 else ""

    reason = _sanitize_reason(raw_reason)

    return SlashCommand(
        name=command,
        reason=reason,
        raw_body=body,
        finding_id=finding_id,
        commit_sha=commit_sha,
    )


def parse_commands(body: str) -> list[SlashCommand | ParseError]:
    """Parse every ``/ai-pr-review`` line in *body*, not just the first.

    A single top-level PR comment can carry several commands, one per line
    (e.g. the top-level-comment workaround for replying to individual inline
    findings — issue #733). ``parse_command`` deliberately only looks at the
    first line (see its docstring and ``test_multiline_body_only_first_line``)
    because most callers re-parse a comment body already known to carry
    exactly one command. This function is for the opposite case: callers that
    need to discover and act on *all* of them.

    Each qualifying line (one starting with the ``/ai-pr-review`` prefix,
    after stripping surrounding whitespace) is parsed independently by
    feeding just that line to ``parse_command`` -- so each returned
    ``SlashCommand.raw_body`` is that single line, not the whole comment.
    Lines that don't start with the prefix are skipped entirely (not even
    reported as errors), matching ``parse_command``'s ``None`` return for a
    non-command body.
    """
    results: list[SlashCommand | ParseError] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(_PREFIX):
            continue
        parsed = parse_command(line)
        if parsed is not None:
            results.append(parsed)
    return results
