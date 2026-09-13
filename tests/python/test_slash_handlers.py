"""Tests for ai_pr_review.slash.handlers — E3.S7."""

from __future__ import annotations

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.slash.handlers import (
    build_entry,
    handle_command,
    list_matching_commands,
    parse_command_gate_lines,
    parse_error_reply,
    run_slash_command,
)
from ai_pr_review.slash.parser import ParseError, SlashCommand


class _RecordingStore:
    """In-memory FeedbackStore that records appends.  ``store_ok`` controls whether
    append() reports success — used to exercise the failure-reply path."""

    def __init__(self, store_ok: bool = True) -> None:
        self.appended: list[FeedbackEntry] = []
        self.store_ok = store_ok

    def append(self, entry: FeedbackEntry) -> bool:
        self.appended.append(entry)
        return self.store_ok

    def load_recent(self) -> list[FeedbackEntry]:
        return list(self.appended)


def _cmd(name: str = "false-positive", reason: str = "test reason") -> SlashCommand:
    return SlashCommand(name=name, reason=reason, raw_body="/ai-pr-review " + name)


def test_build_entry_sets_canonical_name() -> None:
    """build_entry must use the canonical name (dismiss → false-positive)."""
    cmd = _cmd("dismiss", "looks fine")
    entry = build_entry(cmd, source="code-reviewer", file="src/foo.py")
    assert entry.command == "false-positive"
    assert entry.reason == "looks fine"
    assert entry.source == "code-reviewer"
    assert entry.file == "src/foo.py"


def test_handle_feedback_command_persists_and_acks() -> None:
    store = _RecordingStore(store_ok=True)
    cmd = _cmd("false-positive", "intentional")
    entry = build_entry(cmd, source="code-reviewer", file="src/foo.py")

    reply = handle_command(cmd, entry, store)

    assert len(store.appended) == 1
    assert store.appended[0].command == "false-positive"
    assert "recorded" in reply.lower()
    assert "intentional" in reply


def test_handle_feedback_command_reports_persistence_failure() -> None:
    """Regression: when the store fails to persist, the reply must say so
    rather than falsely claiming success."""
    store = _RecordingStore(store_ok=False)
    cmd = _cmd("feedback", "noise reduction please")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert len(store.appended) == 1
    # Reply must NOT falsely claim success
    assert "could not persist" in reply.lower() or "retry" in reply.lower()


def test_build_entry_captures_finding_id_in_extras() -> None:
    """finding_id from the parser must be stored in extras, not dropped."""
    cmd = SlashCommand(
        name="false-positive",
        reason="intentional",
        raw_body="/ai-pr-review false-positive F7 intentional",
        finding_id=7,
    )
    entry = build_entry(cmd, source="code-reviewer", file="src/foo.py")
    assert entry.extras.get("finding_id") == 7


def test_build_entry_no_finding_id_leaves_extras_clean() -> None:
    """When finding_id is None, extras must not contain a finding_id key
    (or any unexpected noise)."""
    cmd = _cmd("false-positive", "no id here")
    entry = build_entry(cmd, source="code-reviewer", file="src/foo.py")
    assert "finding_id" not in entry.extras
    assert "context_missing" not in entry.extras


def test_build_entry_context_missing_flag() -> None:
    """When context_missing=True, extras must carry the flag and optional reason."""
    cmd = _cmd("false-positive", "looks fine")
    entry = build_entry(
        cmd,
        context_missing=True,
        context_missing_reason="parent comment not from bot",
    )
    assert entry.source == ""
    assert entry.file == ""
    assert entry.extras.get("context_missing") is True
    assert entry.extras.get("context_missing_reason") == "parent comment not from bot"


def test_build_entry_context_missing_without_reason() -> None:
    """context_missing=True without a reason must still set the flag."""
    cmd = _cmd("wont-fix", "")
    entry = build_entry(cmd, context_missing=True)
    assert entry.extras.get("context_missing") is True
    assert "context_missing_reason" not in entry.extras


def test_build_entry_context_missing_false_no_flag() -> None:
    """context_missing=False (default) must not pollute extras."""
    cmd = _cmd("false-positive", "reason")
    entry = build_entry(cmd, source="code-reviewer", file="src/foo.py")
    assert "context_missing" not in entry.extras


def test_build_entry_finding_id_and_context_missing_coexist() -> None:
    """finding_id and context_missing can appear together in extras."""
    cmd = SlashCommand(
        name="false-positive",
        reason="",
        raw_body="/ai-pr-review false-positive F3",
        finding_id=3,
    )
    entry = build_entry(
        cmd,
        context_missing=True,
        context_missing_reason="top-level comment, no thread context",
    )
    assert entry.extras.get("finding_id") == 3
    assert entry.extras.get("context_missing") is True
    assert entry.extras.get("context_missing_reason") == "top-level comment, no thread context"


def test_explain_command_returns_stub_reply() -> None:
    store = _RecordingStore()
    cmd = _cmd("explain", "")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert reply  # non-empty
    assert "not yet implemented" in reply.lower() or "explanation" in reply.lower()
    # Explain should not write to the store
    assert store.appended == []


def test_revise_command_returns_stub_reply() -> None:
    store = _RecordingStore()
    cmd = _cmd("revise", "focus on line 42")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert reply
    assert "focus on line 42" in reply or "not yet implemented" in reply.lower()
    assert store.appended == []


def test_feedback_reply_states_finding_remains_open() -> None:
    """Issue #773: 'feedback' only writes an advisory note -- it never
    resolves a thread, dismisses a review, or clears a blocking finding.
    The old wording ("recorded ... Thank you for the feedback.") read as
    confirmation of exactly that, which caused a real PR to sit blocked for
    hours with the reporter believing 'feedback' had cleared it. The reply
    must now say plainly that the finding stays open/blocking and name a
    command that would actually clear it."""
    store = _RecordingStore(store_ok=True)
    cmd = _cmd("feedback", "this is a false positive")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert "remains open" in reply.lower() or "still block" in reply.lower()
    assert "false-positive" in reply
    assert "this is a false positive" in reply  # reason is still echoed


def test_feedback_reply_no_longer_reads_as_confirmation() -> None:
    """Regression guard for the exact misleading phrase issue #773 reported."""
    store = _RecordingStore(store_ok=True)
    cmd = _cmd("feedback", "noise reduction please")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert reply != (
        "**AI Review**: recorded as *feedback*: noise reduction please. "
        "Thank you for the feedback."
    )


def test_false_positive_reply_wording_unchanged() -> None:
    """Regression guard: issue #773 explicitly says do NOT change the
    false-positive/wont-fix reply text, which is accurate as-is (unlike
    feedback's, these commands really do resolve/dismiss elsewhere)."""
    store = _RecordingStore(store_ok=True)
    cmd = _cmd("false-positive", "looks fine to me")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert reply == (
        "**AI Review**: recorded as *false positive*: looks fine to me. "
        "Thank you for the feedback."
    )


def test_wont_fix_reply_wording_unchanged() -> None:
    """Regression guard, same rationale as the false-positive case above."""
    store = _RecordingStore(store_ok=True)
    cmd = _cmd("wont-fix", "intentional design choice")
    entry = build_entry(cmd)

    reply = handle_command(cmd, entry, store)

    assert reply == (
        "**AI Review**: recorded as *won't fix*: intentional design choice. "
        "Thank you for the feedback."
    )


def test_parse_error_reply_names_the_unknown_token() -> None:
    error = ParseError(message="Unknown command 'frobnicate'. Known: [...]", unknown_token="frobnicate")
    reply = parse_error_reply(error)
    assert "frobnicate" in reply
    assert "slash-commands" in reply  # points at the documented grammar
    assert "/ai-pr-review help" in reply


def test_parse_error_reply_falls_back_without_a_token() -> None:
    """Defensive: if some future ParseError construction site omits
    unknown_token, the reply must still render sensibly rather than showing
    an empty pair of backticks."""
    error = ParseError(message="malformed")
    reply = parse_error_reply(error)
    assert "`that`" in reply


def test_fixed_command_never_writes_to_feedback_store() -> None:
    """"fixed" is not a feedback-store verdict (see SlashCommand.is_feedback_command)
    and does not route through handle_command in production -- it rides the
    dismiss/dismiss-inline CLI path instead (ai_pr_review/slash/dismiss.py).
    This locks in defense-in-depth: if handle_command ever receives a
    "fixed" command by accident (e.g. a workflow routing bug), it must fail
    soft -- no store write, no crash -- rather than silently persisting a
    suppression-shaped entry the governance prompt has no rule for.
    """
    store = _RecordingStore()
    cmd = SlashCommand(name="fixed", reason="", raw_body="/ai-pr-review fixed F3 abc1234",
                        finding_id=3, commit_sha="abc1234")
    assert cmd.is_feedback_command is False
    entry = build_entry(cmd)

    handle_command(cmd, entry, store)

    assert store.appended == []


# ---------------------------------------------------------------------------
# run_slash_command / list_matching_commands / parse_command_gate_lines
# (issue #825 -- moved out of ai_pr_review/cli.py's `slash`, `list-commands`,
# and `parse-command` subcommand bodies)
# ---------------------------------------------------------------------------


def test_run_slash_command_flags_context_missing() -> None:
    store = _RecordingStore()
    cmd = SlashCommand(name="wont-fix", reason="intentional", raw_body="/ai-pr-review wont-fix intentional")
    run_slash_command(cmd, store, source="", file="", rule_id="")
    assert len(store.appended) == 1
    assert store.appended[0].extras.get("context_missing") is True


def test_run_slash_command_no_context_missing_with_source() -> None:
    store = _RecordingStore()
    cmd = SlashCommand(name="wont-fix", reason="intentional", raw_body="/ai-pr-review wont-fix intentional")
    run_slash_command(cmd, store, source="code-reviewer", file="app.py", rule_id="")
    assert "context_missing" not in store.appended[0].extras


def test_run_slash_command_returns_handle_command_reply() -> None:
    store = _RecordingStore()
    cmd = SlashCommand(name="feedback", reason="nice catch", raw_body="/ai-pr-review feedback nice catch")
    reply = run_slash_command(cmd, store, source="code-reviewer", file="app.py")
    assert "feedback" in reply.lower()


def test_list_matching_commands_filters_by_family() -> None:
    body = "/ai-pr-review dismiss F1\n/ai-pr-review wont-fix F2 not a bug\n/ai-pr-review rescan"
    entries = list_matching_commands(body, "false-positive,wont-fix")
    assert entries == [
        {"command": "dismiss", "finding_id": 1, "line": "/ai-pr-review dismiss F1"},
        {"command": "wont-fix", "finding_id": 2, "line": "/ai-pr-review wont-fix F2 not a bug"},
    ]


def test_list_matching_commands_empty_body() -> None:
    assert list_matching_commands("", "false-positive") == []


def test_parse_command_gate_lines_unrecognized() -> None:
    assert parse_command_gate_lines("/ai-pr-review frobnicate") == [
        "command=frobnicate",
        "valid=false",
        "unrecognized=true",
    ]


def test_parse_command_gate_lines_bash_only_command() -> None:
    assert parse_command_gate_lines("/ai-pr-review rescan") == ["command=rescan", "valid=true"]


def test_parse_command_gate_lines_feedback_is_invalid() -> None:
    assert parse_command_gate_lines("/ai-pr-review feedback nice work") == [
        "command=feedback",
        "valid=false",
    ]


def test_parse_command_gate_lines_known_command_with_finding_id() -> None:
    assert parse_command_gate_lines("/ai-pr-review dismiss F3 reason text") == [
        "command=dismiss",
        "valid=true",
        "finding_id=3",
    ]


def test_parse_command_gate_lines_bare_body() -> None:
    assert parse_command_gate_lines("") == ["valid=false", "unrecognized=true"]
