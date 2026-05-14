"""Tests for ai_pr_review.slash.parser — E3.S7."""

import pytest

from ai_pr_review.slash.parser import (
    KNOWN_COMMANDS,
    ParseError,
    SlashCommand,
    _sanitize_reason,
    parse_command,
)


# ---------------------------------------------------------------------------
# parse_command happy paths
# ---------------------------------------------------------------------------

def test_false_positive_no_reason() -> None:
    cmd = parse_command("/ai-pr-review false-positive")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "false-positive"
    assert cmd.reason == ""
    assert cmd.canonical_name == "false-positive"


def test_false_positive_with_reason() -> None:
    cmd = parse_command("/ai-pr-review false-positive this is a test")
    assert isinstance(cmd, SlashCommand)
    assert cmd.reason == "this is a test"


def test_wont_fix() -> None:
    cmd = parse_command("/ai-pr-review wont-fix intentional behavior")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "wont-fix"
    assert cmd.canonical_name == "wont-fix"
    assert cmd.reason == "intentional behavior"


def test_explain() -> None:
    cmd = parse_command("/ai-pr-review explain")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "explain"
    assert cmd.reason == ""


def test_revise() -> None:
    cmd = parse_command("/ai-pr-review revise please focus on line 42")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "revise"
    assert cmd.reason == "please focus on line 42"


def test_feedback() -> None:
    cmd = parse_command("/ai-pr-review feedback this rule is too noisy")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "feedback"
    assert cmd.reason == "this rule is too noisy"


def test_dismiss_alias() -> None:
    cmd = parse_command("/ai-pr-review dismiss not relevant here")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "dismiss"
    assert cmd.canonical_name == "false-positive"


def test_multiline_body_only_first_line() -> None:
    body = "/ai-pr-review feedback good\nsome other text\nmore lines"
    cmd = parse_command(body)
    assert isinstance(cmd, SlashCommand)
    assert cmd.reason == "good"


def test_raw_body_preserved() -> None:
    body = "/ai-pr-review feedback hello"
    cmd = parse_command(body)
    assert isinstance(cmd, SlashCommand)
    assert cmd.raw_body == body


def test_command_is_case_insensitive() -> None:
    cmd = parse_command("/ai-pr-review FALSE-POSITIVE")
    assert isinstance(cmd, SlashCommand)
    assert cmd.name == "false-positive"


# ---------------------------------------------------------------------------
# is_feedback_command property
# ---------------------------------------------------------------------------

def test_is_feedback_command_true() -> None:
    for name in ("false-positive", "wont-fix", "feedback"):
        cmd = parse_command(f"/ai-pr-review {name} reason")
        assert isinstance(cmd, SlashCommand)
        assert cmd.is_feedback_command is True


def test_is_feedback_command_false_for_explain() -> None:
    cmd = parse_command("/ai-pr-review explain")
    assert isinstance(cmd, SlashCommand)
    assert cmd.is_feedback_command is False


def test_dismiss_is_feedback_command() -> None:
    cmd = parse_command("/ai-pr-review dismiss")
    assert isinstance(cmd, SlashCommand)
    assert cmd.is_feedback_command is True


# ---------------------------------------------------------------------------
# No-op cases — not a slash command
# ---------------------------------------------------------------------------

def test_empty_body_returns_none() -> None:
    assert parse_command("") is None


def test_unrelated_body_returns_none() -> None:
    assert parse_command("LGTM!") is None


def test_bare_prefix_returns_none() -> None:
    assert parse_command("/ai-pr-review") is None


def test_prefix_with_spaces_only_returns_none() -> None:
    assert parse_command("/ai-pr-review   ") is None


# ---------------------------------------------------------------------------
# ParseError cases
# ---------------------------------------------------------------------------

def test_unknown_command_returns_parse_error() -> None:
    result = parse_command("/ai-pr-review frobnicate")
    assert isinstance(result, ParseError)
    assert "frobnicate" in result.message
    assert result.raw_body == "/ai-pr-review frobnicate"


# ---------------------------------------------------------------------------
# _sanitize_reason
# ---------------------------------------------------------------------------

def test_sanitize_strips_control_chars() -> None:
    raw = "hello\x00world\x01end"
    result = _sanitize_reason(raw)
    assert "\x00" not in result
    assert "\x01" not in result
    assert "hello" in result
    assert "world" in result


def test_sanitize_collapses_newlines() -> None:
    raw = "line1\nline2\r\nline3"
    result = _sanitize_reason(raw)
    assert "\n" not in result
    assert "line1" in result
    assert "line2" in result


def test_sanitize_caps_length() -> None:
    raw = "x" * 2000
    result = _sanitize_reason(raw)
    # After HTML escape 'x' is still 'x', so length should be 1024
    assert len(result) <= 1024


def test_sanitize_html_escapes() -> None:
    raw = '<script>alert("xss")</script>'
    result = _sanitize_reason(raw)
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_sanitize_rejects_api_key_pattern() -> None:
    raw = "api_key=supersecretvalue123"
    result = _sanitize_reason(raw)
    assert result == ""


def test_sanitize_rejects_sk_token() -> None:
    raw = "sk-abcdefghijklmnopqrstuvwxyz12345"
    result = _sanitize_reason(raw)
    assert result == ""


def test_sanitize_rejects_ghp_token() -> None:
    raw = "ghp_" + "a" * 40
    result = _sanitize_reason(raw)
    assert result == ""


def test_sanitize_allows_normal_reason() -> None:
    raw = "This finding is intentional — we use MD5 for non-security checksums."
    result = _sanitize_reason(raw)
    assert "intentional" in result
    assert "checksums" in result


def test_sanitize_nfc_normalizes() -> None:
    # 'é' as NFD (e + combining accent) vs NFC ('é' as single codepoint)
    nfd = "é"  # e + combining acute accent
    result = _sanitize_reason(nfd)
    # Should produce NFC 'é'
    assert result == "\xe9"


# ---------------------------------------------------------------------------
# KNOWN_COMMANDS set
# ---------------------------------------------------------------------------

def test_known_commands_contains_all() -> None:
    expected = {"false-positive", "wont-fix", "explain", "revise", "feedback", "dismiss"}
    assert KNOWN_COMMANDS == expected
