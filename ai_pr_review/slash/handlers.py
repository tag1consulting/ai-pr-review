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
from ai_pr_review.slash.parser import ParseError, SlashCommand

logger = logging.getLogger(__name__)

# Published docs site (docs/_config.yml); README.md links to the same host
# for external-facing doc references.
_SLASH_COMMANDS_DOC_URL = "https://tag1consulting.github.io/ai-pr-review/slash-commands"


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


def run_slash_command(
    result: SlashCommand,
    store: FeedbackStore,
    *,
    source: str = "",
    file: str = "",
    rule_id: str = "",
    context_missing_reason: str = "",
) -> str:
    """Build the FeedbackEntry for a parsed slash command and dispatch it.

    Backs `ai_pr_review.cli`'s `slash` subcommand (issue #825 -- moved out of
    cli.py, which is left as option parsing / config+logging setup / the
    ParseError early-exit only). Wraps `build_entry` + `handle_command` with
    the context-missing detection that previously lived inline in the CLI
    command body: when a feedback command (false-positive/wont-fix/feedback)
    arrives with no source, no file, and no finding_id, the entry is still
    persisted but flagged (`extras["context_missing"]`) so low-fidelity
    records can be filtered/audited later, and a warning is logged so the gap
    surfaces in workflow logs.
    """
    context_missing = (
        result.is_feedback_command
        and not source
        and not file
        and result.finding_id is None
    )
    if context_missing:
        logger.warning(
            "slash: persisting feedback entry with no finding context "
            "(source and file are both empty); command=%r reason=%r%s",
            result.canonical_name,
            result.reason,
            f" context_missing_reason={context_missing_reason!r}" if context_missing_reason else "",
        )

    entry = build_entry(
        result,
        source=source,
        file=file,
        rule_id=rule_id,
        context_missing=context_missing,
        context_missing_reason=context_missing_reason,
    )
    return handle_command(result, entry, store)


def list_matching_commands(comment_body: str, families: str) -> list[dict[str, Any]]:
    """Return every command line in *comment_body* matching one of *families*.

    Backs `ai_pr_review.cli`'s `list-commands` subcommand (issue #825 -- moved
    out of cli.py). Returns ``[{"command": "...", "finding_id": N_or_None,
    "line": "..."}, ...]`` -- one entry per qualifying line, in the order they
    appear. ``command`` is the literal token the user typed (e.g. "dismiss",
    not its canonical "false-positive"), suitable for passing straight back
    as ``--command`` to the ``dismiss`` subcommand. ``line`` is that single
    source line verbatim, suitable for passing as ``--comment-body`` so a
    re-parse of just that line extracts the right reason/finding-id/sha.

    Lines that don't start with the ``/ai-pr-review`` prefix, that fail to
    parse, or whose canonical command isn't in *families* are silently
    omitted -- one malformed or unrelated line must never block its siblings
    (issue #733: a single comment can carry several independent commands).
    Never raises; an empty or absent body returns ``[]``.

    *families* is a comma-separated list of canonical command names, e.g.
    ``"false-positive,wont-fix,fixed"``. A command's canonical name
    normalizes the ``dismiss`` alias to ``false-positive`` (see
    ``SlashCommand.canonical_name``), so ``"false-positive"`` also matches
    lines typed as ``dismiss``.
    """
    from ai_pr_review.slash.parser import parse_commands

    wanted = {f.strip() for f in families.split(",") if f.strip()}
    entries: list[dict[str, Any]] = []
    for parsed in parse_commands(comment_body) if comment_body else []:
        if not isinstance(parsed, SlashCommand):
            continue
        if parsed.canonical_name not in wanted:
            continue
        entries.append({"command": parsed.name, "finding_id": parsed.finding_id, "line": parsed.raw_body})
    return entries


def parse_command_gate_lines(comment_body: str) -> list[str]:
    """Job-routing parse for slash-commands.yml (issue #821).

    Backs `ai_pr_review.cli`'s `parse-command` subcommand (issue #825 --
    moved out of cli.py). See that command's own docstring for the full
    contract (keys emitted, case-normalization behavior, and the exception
    guard's history -- issue #821 / review finding F1 on PR #829). Returns
    the ``key=value`` lines to print, in order; the CLI wrapper echoes each
    one via ``click.echo`` so stdout output is byte-for-byte identical to
    the pre-move implementation (one line per list element, in the same
    order the original function's individual ``click.echo`` calls produced).
    """
    from ai_pr_review.slash.parser import BASH_ONLY_COMMANDS, ParseError, parse_command

    try:
        result = parse_command(comment_body)
    except Exception:
        # Defense-in-depth, not a reachability guarantee either way. The
        # concretely known trigger: parser.py's F<n> regex (_FID_RE) has no
        # digit-count cap, unlike the length-capped regex ([0-9]{1,6}) the
        # three bash steps this replaces used for the same extraction. An
        # absurdly long numeral in the F-ID position (thousands of digits)
        # exceeds Python's int-string conversion limit and raises ValueError
        # from int() deep inside parse_command(). The bash steps never
        # crashed on such input -- the capped regex just failed to match and
        # the digits fell through as ordinary reason text. Replicating that
        # exact fallback would mean reaching inside parse_command() a second
        # time; reject the whole line instead.
        #
        # Caught broadly rather than `except ValueError` (review finding F1,
        # PR #829): this step's contract is "never crashes" regardless of
        # what parser.py does internally -- narrowing to the one exception
        # type known today would leave the same crash-on-malformed-input
        # risk open for any future change inside parse_command() that raises
        # something else (e.g. a future stricter validation). Logged so a
        # genuinely unexpected failure here is still visible in the job log
        # rather than silently swallowed.
        logger.warning("parse-command: unexpected error parsing comment body", exc_info=True)
        return ["valid=false", "unrecognized=true"]

    if result is None:
        # Bare "/ai-pr-review" with nothing after, or a body that doesn't
        # start with the prefix at all. Every job's own `if:` gate already
        # requires the prefix before this step runs, so this is a defensive
        # fallback, not the expected path.
        return ["valid=false", "unrecognized=true"]

    if isinstance(result, ParseError):
        token = result.unknown_token
        if token in BASH_ONLY_COMMANDS:
            return [f"command={token}", "valid=true"]
        lines = [f"command={token}"] if token else []
        lines += ["valid=false", "unrecognized=true"]
        return lines

    # Recognized by ai_pr_review.slash.parser.KNOWN_COMMANDS.
    lines = [f"command={result.name}"]
    if result.name == "feedback":
        lines.append("valid=false")
        return lines
    lines.append("valid=true")
    lines.append(f"finding_id={result.finding_id if result.finding_id is not None else ''}")
    return lines


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
    reason_part = f": {command.reason}" if command.reason else ""

    if command.canonical_name == "feedback":
        # Issue #773: "feedback" only ever writes an advisory note to the
        # learning-loop store -- it never resolves a thread, dismisses a
        # review, or clears a blocking finding (unlike false-positive/
        # wont-fix/dismiss, whose reply lives separately in
        # ai_pr_review/slash/dismiss.py and is NOT changed by this branch).
        # The old wording ("recorded ... Thank you for the feedback.") read
        # as confirmation that the finding was handled, which is exactly
        # what caused a PR to sit blocked for hours with the reporter
        # believing "feedback" had cleared it. State plainly that it hasn't.
        return (
            f"**AI Review**: recorded as *feedback*{reason_part}. This is advisory "
            "only -- the finding remains open and still blocks the review. Reply "
            "with `/ai-pr-review false-positive` (or `dismiss`/`wont-fix`) to "
            "clear it."
        )

    label = {
        "false-positive": "false positive",
        "wont-fix": "won't fix",
    }.get(command.canonical_name, command.canonical_name)
    return f"**AI Review**: recorded as *{label}*{reason_part}. Thank you for the feedback."


def parse_error_reply(error: ParseError) -> str:
    """Build a user-facing reply for a ``ParseError`` (issue #772).

    A malformed ``/ai-pr-review`` command previously failed completely
    silently: the CLI exited non-zero with a technical message on stderr,
    the calling workflow logged a notice and discarded it, and the only
    reaction the commenter ever saw was the unconditional "eyes" reaction
    posted before parsing even ran. This gives the commenter something to
    act on: the token that wasn't recognized, and where to find the
    supported grammar.
    """
    token = error.unknown_token or "that"
    return (
        f"**AI Review**: I didn't recognize `{token}` as a command. See "
        f"{_SLASH_COMMANDS_DOC_URL} for the supported commands, or reply "
        "with `/ai-pr-review help` for a quick summary."
    )
