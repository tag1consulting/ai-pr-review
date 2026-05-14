"""Tests for ai_pr_review.feedback.models — E3.S8."""

from ai_pr_review.feedback.models import FeedbackEntry


def _make_entry(**kwargs: object) -> FeedbackEntry:
    defaults = dict(
        ts="2026-05-14T00:00:00Z",
        command="false-positive",
        reason="test reason",
        source="code-reviewer",
        file="src/foo.py",
        rule_id="",
    )
    defaults.update(kwargs)
    return FeedbackEntry(**defaults)  # type: ignore[arg-type]


def test_to_json_round_trips() -> None:
    entry = _make_entry(rule_id="E501", extras={"pr": 42})
    line = entry.to_json()
    recovered = FeedbackEntry.from_json(line)
    assert recovered is not None
    assert recovered.ts == entry.ts
    assert recovered.command == entry.command
    assert recovered.reason == entry.reason
    assert recovered.source == entry.source
    assert recovered.file == entry.file
    assert recovered.rule_id == entry.rule_id
    assert recovered.extras.get("pr") == 42


def test_from_json_bad_line() -> None:
    assert FeedbackEntry.from_json("not json at all") is None


def test_from_json_non_dict() -> None:
    assert FeedbackEntry.from_json("[1, 2, 3]") is None


def test_from_json_missing_fields_ok() -> None:
    """from_json should tolerate missing optional fields (forward compat)."""
    minimal = '{"ts": "2026-01-01T00:00:00Z", "command": "feedback", "reason": "", "source": ""}'
    entry = FeedbackEntry.from_json(minimal)
    assert entry is not None
    assert entry.file == ""
    assert entry.rule_id == ""
    assert entry.extras == {}


def test_from_json_unknown_fields_go_to_extras() -> None:
    line = '{"ts":"2026-01-01T00:00:00Z","command":"feedback","reason":"r","source":"s","future_field":"v"}'
    entry = FeedbackEntry.from_json(line)
    assert entry is not None
    assert entry.extras.get("future_field") == "v"
