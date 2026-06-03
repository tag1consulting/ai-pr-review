"""Slash command handlers — E3.S7.

Dispatches parsed ``SlashCommand`` objects to the appropriate action:

- ``false-positive`` / ``dismiss`` / ``wont-fix`` / ``feedback``:
  Write a ``FeedbackEntry`` to the configured store (GitHub-only now).
- ``explain``:
  Re-invoke the originating agent with a request for a detailed explanation.
  (Stubbed — full agent re-invocation is out of scope for E3; returns a
  canned message so the workflow can post it as a reply comment.)
- ``revise``:
  Re-invoke the originating agent with the user-supplied hint.
  (Stubbed — same rationale as ``explain``.)

The caller (CLI ``slash`` subcommand or the GHA step in slash-commands.yml)
is responsible for:
  1. Parsing the comment body into a ``SlashCommand`` via ``parse_command()``.
  2. Building a ``FeedbackEntry`` with context (source, file, rule_id, ts).
  3. Calling ``handle_command(command, entry, store)``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.feedback.store import FeedbackStore
from ai_pr_review.slash.parser import SlashCommand

logger = logging.getLogger(__name__)


def build_entry(
    command: SlashCommand,
    *,
    source: str = "",
    file: str = "",
    rule_id: str = "",
    context_missing: bool = False,
    context_missing_reason: str = "",
) -> FeedbackEntry:
    """Build a ``FeedbackEntry`` from a parsed ``SlashCommand``.

    Parameters
    ----------
    command:
        Parsed slash command carrying ``reason`` and optional ``finding_id``.
    source:
        Finding source tag (e.g. ``code-reviewer``, ``sarif:bandit``).
        Populated by the GHA workflow from the parent comment header; empty
        when context extraction failed or the command was a top-level comment.
    file:
        File path the finding was on. Same caveats as *source*.
    rule_id:
        SARIF rule ID; only meaningful for ``sarif:*`` sources.
    context_missing:
        When ``True``, both *source* and *file* were unavailable and the
        entry is being persisted with reduced fidelity.  The ``extras`` dict
        will carry ``{"context_missing": True}`` (and optionally
        ``context_missing_reason``) so callers can filter these records.
    context_missing_reason:
        Human-readable explanation of why context is absent, forwarded from
        the GHA ``context_missing_reason`` output.
    """
    extras: dict[str, Any] = {}
    if command.finding_id is not None:
        extras["finding_id"] = command.finding_id
    if context_missing:
        extras["context_missing"] = True
        if context_missing_reason:
            extras["context_missing_reason"] = context_missing_reason
    return FeedbackEntry(
        ts=datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        command=command.canonical_name,
        reason=command.reason,
        source=source,
        file=file,
        rule_id=rule_id,
        extras=extras,
    )


def handle_command(
    command: SlashCommand,
    entry: FeedbackEntry,
    store: FeedbackStore,
) -> str:
    """Execute *command* and return a reply message for the comment thread.

    Parameters
    ----------
    command:
        Parsed and sanitized slash command.
    entry:
        Pre-built FeedbackEntry (caller provides context like source, file).
    store:
        The feedback store to write to (may be UnsupportedVcsStore).

    Returns
    -------
    A short reply string to post back to the comment thread, or an empty
    string when no reply is appropriate.
    """
    name = command.canonical_name

    if command.is_feedback_command:
        stored = store.append(entry)
        if stored:
            logger.info(
                "slash: stored feedback command=%r source=%r file=%r",
                name,
                entry.source,
                entry.file,
            )
            return _feedback_reply(command)
        # Store could not persist (network, missing branch, unsupported VCS).
        # Tell the user honestly rather than silently lying.
        logger.warning(
            "slash: feedback store failed to persist command=%r", name,
        )
        return (
            "**AI Review**: your command was received, but the feedback store "
            "could not persist it (network error or unsupported VCS). "
            "Please retry later or check the workflow logs for details."
        )

    if name == "explain":
        logger.info("slash: explain command — stub reply (full re-invocation deferred)")
        return (
            "**AI Review**: explanation re-invocation is not yet implemented. "
            "Please file an issue or open a discussion if you need more detail."
        )

    if name == "revise":
        logger.info("slash: revise command — stub reply (full re-invocation deferred)")
        hint = command.reason or "(no hint provided)"
        return (
            f"**AI Review**: revision with hint `{hint}` is not yet implemented. "
            "Please file an issue or open a discussion if you would like this feature."
        )

    # Should never reach here — parser only returns known commands
    logger.warning("slash: unhandled command %r; ignoring", name)
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _feedback_reply(command: SlashCommand) -> str:
    label = {
        "false-positive": "false positive",
        "wont-fix": "won't fix",
        "feedback": "feedback",
    }.get(command.canonical_name, command.canonical_name)

    reason_part = f": {command.reason}" if command.reason else ""
    return f"**AI Review**: recorded as *{label}*{reason_part}. Thank you for the feedback."
