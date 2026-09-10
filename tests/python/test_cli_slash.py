"""Tests for the `ai-pr-review slash` CLI subcommand's ParseError handling
(issue #772).

Before this fix, a ParseError produced a technical message on stderr and
exit code 2 with NOTHING on stdout. The calling workflow captures stdout as
the reply to post back to the PR comment thread, so a malformed command was
discarded completely silently downstream of this CLI call. This locks in
that a ParseError now ALSO prints a short, user-facing explanation to
stdout, while still exiting 2 and still writing the technical message to
stderr (unchanged, for the job log).
"""

from __future__ import annotations

from click.testing import CliRunner

from ai_pr_review.cli import cli


def _run(body: str) -> object:
    runner = CliRunner()
    return runner.invoke(cli, ["slash", "--body", body])


def test_unknown_command_exits_2_and_prints_reply_to_stdout() -> None:
    result = _run("/ai-pr-review frobnicate something")
    assert result.exit_code == 2
    assert "frobnicate" in result.stdout
    assert "slash-commands" in result.stdout


def test_unknown_command_stderr_still_has_technical_message() -> None:
    """The stderr message (job-log-facing, not PR-facing) is unchanged."""
    result = _run("/ai-pr-review frobnicate something")
    assert result.exit_code == 2
    assert "Unknown command" in result.stderr


def test_argument_order_repro_from_issue_772() -> None:
    """The exact repro from issue #772's report: argument order inverted so
    the first token is an F-id rather than a verb."""
    result = _run("/ai-pr-review F2 dismiss we don't care about this")
    assert result.exit_code == 2
    assert "f2" in result.stdout.lower()


def test_not_a_slash_command_is_a_silent_noop() -> None:
    """A body that isn't a /ai-pr-review command at all must remain a true
    no-op: exit 0, nothing on stdout. Only a recognized-prefix-but-malformed
    command gets the new stdout message."""
    result = _run("just a regular comment")
    assert result.exit_code == 0
    assert result.stdout == ""


def test_empty_body_is_a_silent_noop() -> None:
    result = _run("")
    assert result.exit_code == 0
    assert result.stdout == ""
