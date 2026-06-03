"""Tests for ai_pr_review.slash.handlers — E3.S7."""

from __future__ import annotations

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.slash.handlers import build_entry, handle_command
from ai_pr_review.slash.parser import SlashCommand


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
