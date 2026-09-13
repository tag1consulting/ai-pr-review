"""Structural guard against issue #769's bug class: `.github/workflows/
slash-commands.yml` running two independent jobs' reply-and-store-write logic
for the same (event type, verdict command) pair. Neither `dismiss-body-
finding`/`dismiss-finding` `needs:` `feedback-command` (or vice versa), so if
their effective command families ever overlap again, both act on the same
comment silently -- a double reply and, on the body-level path, a double
feedback-store write, exactly as issue #769 found live on PR #768.

This does NOT evaluate the workflow's `if:` expressions semantically (no
GitHub Actions expression engine here) -- like
`test_container_action_env.py`'s regex-scoped approach, it uses `yaml.
safe_load` only to locate each job/step (so extraction is never fooled by a
job name or step name that happens to appear inside a comment elsewhere in
the file), then regexes the specific family lists each one's own text
declares (a job-level `contains(fromJSON(...), ...)`/`startsWith(...)`
chain, a `--families ...` CLI argument, or a step-level exclusion array).
It asserts those lists don't overlap for the verdict family this issue is
about (dismiss/false-positive/wont-fix/fixed, normalized to canonical
names). Its value is catching a future family/job edit that reintroduces
the overlap, not proving the `if:` conditions are correct in general.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ai_pr_review.slash.parser import BASH_ONLY_COMMANDS

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "slash-commands.yml"
)

# Commands handle-command's job-routing parse recognizes that ai_pr_review.
# slash.parser's KNOWN_COMMANDS never sees at all -- rescan/review-full/skip/
# help are a bash-only vocabulary this job fully owns. Issue #821: imported
# from parser.py (single source of truth) rather than duplicated here, same
# as KNOWN_COMMANDS above.
_BASH_ONLY_COMMANDS = BASH_ONLY_COMMANDS

# `dismiss`/canonical `false-positive` are the same family everywhere in this
# repo (ai_pr_review/slash/parser.py's SlashCommand.canonical_name); normalize
# so a job spelled with one and another spelled with the other still compare
# as the same family.
_ALIAS_TO_CANONICAL = {"dismiss": "false-positive"}


def _canonicalize(commands: set[str]) -> set[str]:
    return {_ALIAS_TO_CANONICAL.get(c, c) for c in commands}


def _workflow_jobs() -> dict[str, Any]:
    assert _WORKFLOW_PATH.is_file(), f"expected workflow at {_WORKFLOW_PATH}"
    doc = yaml.safe_load(_WORKFLOW_PATH.read_text())
    return doc["jobs"]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found; steps present: {[s.get('name') for s in job['steps']]}")


def test_workflow_parses_and_has_expected_jobs() -> None:
    jobs = _workflow_jobs()
    assert set(jobs) == {
        "authorize",
        "handle-command",
        "dismiss-body-finding",
        "dismiss-finding",
        "feedback-command",
    }


def _dismiss_body_finding_families(jobs: dict[str, Any]) -> set[str]:
    """`dismiss-body-finding`'s "List dismiss commands" step -- the only step
    that both replies AND (per cli.py's `dismiss` command) can write to the
    feedback store, for the `issue_comment` event."""
    step = _step(jobs["dismiss-body-finding"], "List dismiss commands")
    match = re.search(r"ai-pr-review list-commands --families ([\w,-]+)", step["run"])
    assert match, "could not find dismiss-body-finding's --families argument"
    return _canonicalize(set(match.group(1).split(",")))


def _dismiss_finding_families(jobs: dict[str, Any]) -> set[str]:
    """`dismiss-finding`'s job-level `if:` startsWith chain -- the whole job
    exists only to reply to and (per cli.py's `dismiss-inline` command) write
    to the feedback store for these commands, for the
    `pull_request_review_comment` event."""
    if_text = jobs["dismiss-finding"]["if"]
    commands = set(re.findall(r"startsWith\(inputs\.comment-body, '/ai-pr-review ([\w-]+)'\)", if_text))
    assert commands, "could not extract any commands from dismiss-finding's if: block"
    return _canonicalize(commands)


def _feedback_command_top_level_families(jobs: dict[str, Any]) -> set[str]:
    """`feedback-command`'s "List feedback commands" step -- the top-level
    (`issue_comment`) per-line loop's `--families` argument."""
    step = _step(jobs["feedback-command"], "List feedback commands")
    match = re.search(r"ai-pr-review list-commands --families ([\w,-]+)", step["run"])
    assert match, "could not find feedback-command's top-level --families argument"
    return _canonicalize(set(match.group(1).split(",")))


def _feedback_command_review_thread_excluded_families(jobs: dict[str, Any]) -> set[str]:
    """The step-level exclusion this fix (issue #769) added to
    `feedback-command`'s review-thread (`pull_request_review_comment`) path:
    commands in this set never reach "Extract finding context"/"Invoke Python
    slash handler", because `dismiss-finding` already owns them on this event
    type."""
    step = _step(jobs["feedback-command"], "Extract finding context")
    matches = re.findall(
        r"!contains\(fromJSON\('\[([^\]]+)\]'\), steps\.cmd\.outputs\.command\)",
        step["if"],
    )
    assert matches, "could not find feedback-command's step-level command exclusion"
    excluded: set[str] = set()
    for m in matches:
        excluded |= {c.strip().strip('"') for c in m.split(",")}
    return _canonicalize(excluded)


def test_no_overlap_between_dismiss_body_finding_and_feedback_command_top_level() -> None:
    """issue_comment path: dismiss-body-finding and feedback-command's
    top-level loop must never both act on the same verdict family --
    otherwise every matching comment gets two replies and (for a body-level
    F-id) two feedback-store entries, exactly as PR #768 showed live."""
    jobs = _workflow_jobs()
    dismiss_families = _dismiss_body_finding_families(jobs)
    feedback_families = _feedback_command_top_level_families(jobs)

    overlap = dismiss_families & feedback_families
    assert overlap == set(), (
        f"dismiss-body-finding and feedback-command's top-level path both act on "
        f"{overlap} -- this reintroduces issue #769's double-reply/double-write bug"
    )
    # Sanity: the extraction itself must be meaningful, not an empty match
    # that would make the assertion above trivially pass.
    assert dismiss_families, "dismiss-body-finding's family extraction returned nothing"
    assert feedback_families, "feedback-command's top-level family extraction returned nothing"


def test_no_overlap_between_dismiss_finding_and_feedback_command_review_thread() -> None:
    """pull_request_review_comment path: dismiss-finding's verdict family
    must be entirely covered by feedback-command's review-thread exclusion
    list, or the same double-reply/double-write bug recurs on inline
    replies."""
    jobs = _workflow_jobs()
    dismiss_families = _dismiss_finding_families(jobs)
    excluded_families = _feedback_command_review_thread_excluded_families(jobs)

    not_excluded = dismiss_families - excluded_families
    assert not_excluded == set(), (
        f"dismiss-finding acts on {not_excluded}, but feedback-command's review-thread "
        "path does not exclude them -- both jobs would reply (and, per cli.py's "
        "dismiss-inline, both could write to the feedback store) for the same inline "
        "verdict comment"
    )


def _parse_command_run_text(jobs: dict[str, Any], job_name: str, step_name: str) -> str:
    return _step(jobs[job_name], step_name)["run"]


def test_all_three_parse_command_steps_call_the_shared_entry_point() -> None:
    """Issue #821: handle-command, dismiss-finding, and feedback-command's
    "Parse command" steps no longer each run their own bash `case`/`awk`
    re-implementation -- they all invoke the same `ai-pr-review
    parse-command` CLI subcommand (backed by `ai_pr_review.slash.parser`'s
    `KNOWN_COMMANDS`/`parse_command`, see test_cli_parse_command.py for its
    own coverage). This guards against a future edit reintroducing a
    bespoke bash re-parse in any of the three jobs -- the exact drift this
    issue closes.

    handle-command's step wraps the call in `docker run` (it runs on a bare
    runner, not inside the ai-pr-review image, unlike the other two jobs --
    see that step's own comment for why); the other two invoke the
    console-script directly since their job already runs inside that image.
    Assert on the shared `parse-command` subcommand name so both invocation
    styles (the `ai-pr-review` console script directly, or `python3 -m
    ai_pr_review` inside `docker run`) are covered by one check.
    """
    jobs = _workflow_jobs()
    handle_run = _parse_command_run_text(jobs, "handle-command", "Parse command")
    dismiss_run = _parse_command_run_text(jobs, "dismiss-finding", "Parse command")
    feedback_run = _parse_command_run_text(
        jobs, "feedback-command", "Parse command (review-thread path only)"
    )

    for job_name, run_text in (
        ("handle-command", handle_run),
        ("dismiss-finding", dismiss_run),
        ("feedback-command", feedback_run),
    ):
        assert re.search(r"ai[_-]pr[_-]review\b.*\bparse-command\b", run_text), (
            f"{job_name}'s Parse command step no longer calls the shared "
            "`parse-command` entry point (issue #821) -- found:\n"
            f"{run_text}"
        )
        assert "case " not in run_text and "esac" not in run_text, (
            f"{job_name}'s Parse command step still contains a bash `case` "
            "statement -- issue #821 replaced all three with the shared "
            "Python entry point"
        )


def test_verdict_family_is_the_expected_four_commands() -> None:
    """Pins the family this issue is about, so a future rename/typo in either
    job's command list is caught even if it happens to keep the two jobs'
    (now-wrong) lists consistent with each other."""
    jobs = _workflow_jobs()
    assert _dismiss_finding_families(jobs) == {"false-positive", "wont-fix", "fixed"}
    # dismiss-body-finding's store-eligible family excludes `fixed` from the
    # written-to-store set per cli.py, but this step's --families argument
    # governs which lines are ACTED on (replied to) at all, and intentionally
    # includes `fixed` (cli.py's dismiss command still replies for `fixed`,
    # just never persists it -- see DismissResult.feedback_eligible).
    assert _dismiss_body_finding_families(jobs) == {"false-positive", "wont-fix", "fixed"}
