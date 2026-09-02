"""Tests for the `ai-pr-review list-commands` CLI subcommand (issue #733).

Thin CLI wrapper over `ai_pr_review.slash.parser.parse_commands`, used by
slash-commands.yml to fan out over every command line in a single PR comment
instead of acting on only the first.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from ai_pr_review.cli import cli


def _run(comment_body: str, families: str) -> list[dict]:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["list-commands", "--comment-body", comment_body, "--families", families]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_lists_one_entry_per_matching_line() -> None:
    body = "/ai-pr-review dismiss F1\n/ai-pr-review wont-fix F2 not a bug\n/ai-pr-review fixed F5 abc1234"
    entries = _run(body, "false-positive,wont-fix,fixed")
    assert entries == [
        {"command": "dismiss", "finding_id": 1, "line": "/ai-pr-review dismiss F1"},
        {"command": "wont-fix", "finding_id": 2, "line": "/ai-pr-review wont-fix F2 not a bug"},
        {"command": "fixed", "finding_id": 5, "line": "/ai-pr-review fixed F5 abc1234"},
    ]


def test_dismiss_alias_matches_false_positive_family() -> None:
    # "dismiss" canonicalizes to "false-positive" -- families filters on the
    # canonical name, so "false-positive" in --families must still surface a
    # literal "dismiss" line (with its original typed name preserved as
    # "command" for the caller to pass straight to --command).
    entries = _run("/ai-pr-review dismiss F1", "false-positive")
    assert entries == [{"command": "dismiss", "finding_id": 1, "line": "/ai-pr-review dismiss F1"}]


def test_fixed_excluded_from_feedback_family() -> None:
    body = "/ai-pr-review fixed F1 abc1234\n/ai-pr-review wont-fix F2 reason"
    entries = _run(body, "false-positive,wont-fix,feedback")
    assert [e["command"] for e in entries] == ["wont-fix"]


def test_unrelated_and_malformed_lines_are_skipped() -> None:
    body = "just a comment\n/ai-pr-review dismiss F1\n/ai-pr-review not-a-real-command\n/ai-pr-review rescan"
    entries = _run(body, "false-positive,wont-fix,fixed")
    assert [e["command"] for e in entries] == ["dismiss"]


def test_empty_body_returns_empty_array() -> None:
    assert _run("", "false-positive,wont-fix,fixed") == []


def test_no_matching_family_returns_empty_array() -> None:
    entries = _run("/ai-pr-review rescan", "false-positive,wont-fix,fixed")
    assert entries == []
