"""Tests for the `ai-pr-review parse-command` CLI subcommand (issue #821).

Job-routing parse shared by all three "Parse command" steps in
slash-commands.yml (handle-command, dismiss-finding, feedback-command) --
see ai_pr_review/cli.py's parse_command_gate docstring for the full output
contract and the deliberate case-insensitivity normalization relative to the
three bash `case` statements it replaces.
"""

from __future__ import annotations

from click.testing import CliRunner

from ai_pr_review.cli import cli
from ai_pr_review.slash.parser import BASH_ONLY_COMMANDS, KNOWN_COMMANDS


def _run(comment_body: str) -> dict[str, str]:
    runner = CliRunner()
    result = runner.invoke(cli, ["parse-command", "--comment-body", comment_body])
    assert result.exit_code == 0, result.output
    parsed: dict[str, str] = {}
    for line in result.output.splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        parsed[key] = value
    return parsed


def test_bash_only_commands_are_recognized_and_valid() -> None:
    for command in sorted(BASH_ONLY_COMMANDS):
        out = _run(f"/ai-pr-review {command}")
        assert out == {"command": command, "valid": "true"}, out


def test_feedback_is_recognized_but_not_valid_here() -> None:
    # Owned entirely by the feedback-command job -- not an error, but not
    # something handle-command/dismiss-finding act on directly either.
    out = _run("/ai-pr-review feedback something broke")
    assert out["command"] == "feedback"
    assert out["valid"] == "false"
    assert "unrecognized" not in out


def test_verdict_family_is_valid_with_finding_id() -> None:
    for command in ("dismiss", "false-positive", "wont-fix", "fixed", "explain", "revise"):
        out = _run(f"/ai-pr-review {command} F3 some reason")
        assert out["command"] == command
        assert out["valid"] == "true"
        assert out["finding_id"] == "3"


def test_verdict_family_without_finding_id_is_empty_not_absent() -> None:
    out = _run("/ai-pr-review dismiss")
    assert out["command"] == "dismiss"
    assert out["valid"] == "true"
    assert out["finding_id"] == ""


def test_bracketed_finding_id_is_accepted() -> None:
    # Issue #735's bracketed form, as shown in review bodies.
    out = _run("/ai-pr-review dismiss [F7]")
    assert out["finding_id"] == "7"


def test_unrecognized_command_sets_unrecognized_and_echoes_token() -> None:
    out = _run("/ai-pr-review frobnicate")
    assert out["command"] == "frobnicate"
    assert out["valid"] == "false"
    assert out["unrecognized"] == "true"


def test_bare_prefix_with_nothing_after_is_unrecognized_with_no_command() -> None:
    out = _run("/ai-pr-review")
    assert "command" not in out
    assert out["valid"] == "false"
    assert out["unrecognized"] == "true"


def test_mixed_case_command_is_normalized_and_recognized() -> None:
    # Deliberate normalization vs. the three bash `case` statements this
    # replaces (see parse_command_gate's docstring): SlashCommand.name is
    # always lowercased, so a mixed-case command is now recognized instead
    # of silently misrouting to the unrecognized-command path.
    out = _run("/ai-pr-review Dismiss")
    assert out["command"] == "dismiss"
    assert out["valid"] == "true"


def test_every_known_command_and_bash_only_command_is_covered() -> None:
    # Issue #772's original intent, preserved: every name in KNOWN_COMMANDS
    # (plus the bash-only vocabulary KNOWN_COMMANDS doesn't cover) must never
    # fall into the unrecognized path.
    for command in sorted(KNOWN_COMMANDS | BASH_ONLY_COMMANDS):
        out = _run(f"/ai-pr-review {command} F1 reason")
        assert "unrecognized" not in out, f"{command!r} was incorrectly treated as unrecognized"


def test_absurdly_long_finding_id_digit_string_does_not_crash() -> None:
    # Defense-in-depth: parser.py's F<n> regex has no digit-count cap, unlike
    # the bash steps' capped [0-9]{1,6}. A numeral long enough to exceed
    # Python's int-string conversion limit must not crash this step.
    out = _run(f"/ai-pr-review dismiss F{'9' * 5000} reason")
    assert out["valid"] == "false"
    assert out["unrecognized"] == "true"


def test_unexpected_exception_from_parse_command_does_not_crash(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Review finding F1 (PR #829): the guard around parse_command() must not
    # be narrowed to just ValueError -- any exception parse_command() could
    # ever raise must still leave this step's "never crashes" contract
    # intact, not just the one concretely-known trigger (an oversized F<n>
    # digit string).
    import ai_pr_review.slash.parser as parser_module

    def _boom(_body: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(parser_module, "parse_command", _boom)
    runner = CliRunner()
    result = runner.invoke(cli, ["parse-command", "--comment-body", "/ai-pr-review dismiss F1"])
    assert result.exit_code == 0, result.output
    out: dict[str, str] = {}
    for line in result.output.splitlines():
        if not line:
            continue
        key, _, value = line.partition("=")
        out[key] = value
    assert out["valid"] == "false"
    assert out["unrecognized"] == "true"
