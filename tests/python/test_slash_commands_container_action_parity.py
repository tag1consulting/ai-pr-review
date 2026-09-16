"""Guards against issue #863's bug class: a `container-action` input has no
effect on `/ai-pr-review rescan`/`review-full` unless it is actually
forwarded from `.github/workflows/slash-commands.yml`'s review-dispatch step,
with a default matching `container-action`'s own. `max-diff-lines`/
`max-inline`/`ignore-merge-commits`/`context-enrichment` existed nowhere in
slash-commands.yml at all before #863; `analyzers`/`exclude-analyzers`/
`agents`/`exclude-agents` were fixed the same way earlier by issue #516; and
`model-standard`/`model-premium`/`parallel`/`max-tokens-per-agent`/
`enable-suggestions`/`fail-on-findings`/`feedback-loop`/`token-usage-display`/
`token-usage-warn-usd`/`max-cost-usd`/`fail-on-cost-ceiling`/
`context-max-queries`/`exclude-patterns`/`exclude-patterns-mode`/
`analyzer-diff-scope` were a third, larger instance of the same gap found
while writing this test (#863 follow-up, issue #865): each one worked on
the automatic `pull_request`-triggered review but had no effect on a
manual rescan. `fail-on-findings` needed a different fix from the other
14: forwarding it with the review job's own 'true' fallback would have
made a normal rescan-with-findings fail the workflow run and misfire the
failure-reaction step, so the slash-commands callers intentionally forward
'false' here instead of mirroring the review job's default (see the
comment beside each `fail-on-findings:` forward in the caller workflows).

This is deliberately an EXEMPTION list, not an allowlist: every
`container-action` input must be forwarded to the review step under the
same name unless it is listed in `_EXEMPT_INPUTS` with a reason. An
allowlist only protects the specific inputs someone remembered to name; it
would have let this exact bug class recur a fourth time. The exemption
list is designed to fail loudly the next time a new `container-action`
input is added and nobody decided whether it belongs here -- the CONTRIBUTING.md
pre-PR checklist points here.

Structural (`yaml.safe_load`), like test_action_yml_input_parity.py and
test_slash_commands_workflow.py: both files' relevant blocks are plain YAML
mappings, so there's no risk of a regex being fooled by a name mentioned in
prose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_SLASH_COMMANDS_YML = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "slash-commands.yml"
)
_CONTAINER_ACTION_YML = Path(__file__).resolve().parents[2] / "container-action" / "action.yml"

_REVIEW_STEP_NAME = "Run review (rescan/review-full)"

# container-action inputs that are deliberately NOT forwarded to the
# slash-command review step, with a reason. Every input NOT listed here
# must be both declared as a workflow_call input on slash-commands.yml and
# forwarded from _REVIEW_STEP_NAME with a matching default. Adding a new
# container-action input to this list is a real decision (see
# CONTRIBUTING.md's pre-PR checklist) -- don't add one just to make this
# test pass.
_EXEMPT_INPUTS: dict[str, str] = {
    # GitHub-context / connection plumbing, not a review-tuning knob. Already
    # forwarded correctly and covered by test_slash_commands_workflow.py /
    # hand-verified elsewhere; exempting here only means "not required to be
    # forwarded under this input's own name via this generic mechanism".
    "image-tag": "image selection, already forwarded",
    "registry-token": "private-GHCR auth; unused by the public ghcr.io image path this workflow pulls",
    "provider": "LLM provider selection, already forwarded",
    "api-key": "secret, forwarded via secrets: not with:",
    "base-url": "provider connection plumbing, already forwarded",
    "github-token": "VCS auth, already forwarded (as actions-token/github-token secrets)",
    "review-mode": "computed per-command (rescan vs review-full), not a passthrough of a single input",
    "pr-number": "GitHub context, already forwarded",
    "base-ref": "GitHub context, already forwarded",
    "head-ref": "GitHub context, already forwarded",
    "head-sha": "GitHub context, already forwarded",
    # Genuinely inapplicable in a slash-command context.
    "review-target": (
        "always implicitly 'pr' when triggered from a PR comment; the only other "
        "value ('standalone') is deprecated (issue #623) and only reachable via "
        "workflow_dispatch, which slash commands never use"
    ),
    "sarif-paths": (
        "points at SARIF artifacts produced by a preceding sarif-prep job that only "
        "runs as part of the pull_request-triggered workflow; the comment-triggered "
        "job has no equivalent artifact to point at without separate plumbing work "
        "(tracked as a possible follow-up, not fixed here)"
    ),
    # Verified (grepped docs/, README.md, .github/, examples/) not wired to
    # an AI_REVIEW_* repo variable anywhere in this repo's own conventions,
    # not even on the automatic pull_request-triggered review, so there is
    # no "works on the automatic review but not on rescan" asymmetry to
    # guard against for these yet. Do not add an input here just because
    # it looks similar to these -- verify it the same way first, the same
    # way this test's own code-review caught 8 inputs that had been
    # exempted with this exact reason while actually already being wired
    # on the review job (fixed, no longer exempt; see the CHANGELOG entry
    # for issue #865).
    "temperature": "not yet exposed via an AI_REVIEW_* repo variable on any review path",
    "analyzer-concurrency": "not yet exposed via an AI_REVIEW_* repo variable on any review path",
    "engine": "not yet exposed via an AI_REVIEW_* repo variable on any review path",
    "judge-pass": "not yet exposed via an AI_REVIEW_* repo variable on any review path",
    "profile-max-tokens": "deprecated no-op, removal planned for v3.0.0",
}


def _load(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"expected {path} to exist"
    return yaml.safe_load(path.read_text())


def _workflow_call_inputs() -> dict[str, Any]:
    doc = _load(_SLASH_COMMANDS_YML)
    # PyYAML's safe_load parses the bare `on:` key as the boolean True under
    # YAML 1.1 rules, not the string "on" -- look it up either way.
    on_block = doc.get("on", doc.get(True))
    assert isinstance(on_block, dict), f"{_SLASH_COMMANDS_YML} has no top-level `on:` mapping"
    inputs = on_block["workflow_call"]["inputs"]
    assert isinstance(inputs, dict), f"{_SLASH_COMMANDS_YML} has no workflow_call.inputs mapping"
    return inputs


def _container_action_inputs() -> dict[str, Any]:
    doc = _load(_CONTAINER_ACTION_YML)
    inputs = doc["inputs"]
    assert isinstance(inputs, dict), f"{_CONTAINER_ACTION_YML} has no top-level inputs mapping"
    return inputs


def _review_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found; steps present: {[s.get('name') for s in job['steps']]}")


def _required_inputs() -> set[str]:
    container_inputs = set(_container_action_inputs())
    unknown_exemptions = set(_EXEMPT_INPUTS) - container_inputs
    assert not unknown_exemptions, (
        f"_EXEMPT_INPUTS lists {sorted(unknown_exemptions)}, which no longer exist in "
        f"{_CONTAINER_ACTION_YML} -- remove the stale entries"
    )
    return container_inputs - set(_EXEMPT_INPUTS)


def test_every_non_exempt_container_action_input_is_a_workflow_call_input() -> None:
    required = _required_inputs()
    workflow_inputs = set(_workflow_call_inputs())

    missing = required - workflow_inputs
    assert not missing, (
        f"{sorted(missing)} exist in {_CONTAINER_ACTION_YML} and are not exempted in "
        f"_EXEMPT_INPUTS, but are not declared as workflow_call inputs on "
        f"{_SLASH_COMMANDS_YML} -- either add them there (and forward them from "
        f"{_REVIEW_STEP_NAME!r}) or add them to _EXEMPT_INPUTS with a reason"
    )


def test_every_non_exempt_input_is_forwarded_from_the_review_step() -> None:
    doc = _load(_SLASH_COMMANDS_YML)
    job = doc["jobs"]["handle-command"]
    step = _review_step(job, _REVIEW_STEP_NAME)
    forwarded = step.get("with", {})

    for name in sorted(_required_inputs()):
        expected = f"${{{{ inputs.{name} }}}}"
        assert forwarded.get(name) == expected, (
            f"{_REVIEW_STEP_NAME!r} does not forward `{name}` to container-action as "
            f"{expected!r} (got {forwarded.get(name)!r}) -- a workflow_call input with "
            "no effect on the actual review is exactly issue #863's bug class"
        )


def test_non_exempt_input_defaults_match_container_action() -> None:
    workflow_inputs = _workflow_call_inputs()
    container_inputs = _container_action_inputs()

    for name in sorted(_required_inputs()):
        assert name in workflow_inputs, f"`{name}` missing from workflow_call.inputs (see the other tests in this file)"
        workflow_default = workflow_inputs[name].get("default")
        container_default = container_inputs[name].get("default")
        assert workflow_default == container_default, (
            f"`{name}` defaults to {workflow_default!r} in {_SLASH_COMMANDS_YML} but "
            f"{container_default!r} in {_CONTAINER_ACTION_YML} -- omitting this input from a "
            "caller workflow would silently change behavior instead of being a no-op"
        )


def test_review_step_still_runs_for_both_rescan_and_review_full() -> None:
    """Merging the two former steps into one made their behavior depend on
    `if:` and `review-mode` expressions instead of two separate steps each
    hard-wired to a single command. Nothing else in this test file reads
    those expressions -- guard them explicitly so a future edit that drops
    one command from the `if:` (silently disabling it) is caught here."""
    doc = _load(_SLASH_COMMANDS_YML)
    job = doc["jobs"]["handle-command"]
    step = _review_step(job, _REVIEW_STEP_NAME)
    if_condition = step.get("if", "")

    for command in ("rescan", "review-full"):
        assert f"'{command}'" in if_condition, (
            f"{_REVIEW_STEP_NAME!r}'s `if:` no longer mentions {command!r} -- this would "
            f"silently stop that command from dispatching a review at all. Current `if:`:\n"
            f"{if_condition}"
        )


def test_review_mode_ternary_preserves_per_command_behavior() -> None:
    """Before the merge, `review-mode` was a literal 'full' on the
    review-full step and `inputs.review-mode-default` on the rescan step --
    two separate, un-editable-together values. The merged step computes the
    same two values with one ternary; pin its exact text so an inverted or
    otherwise broken ternary (e.g. forcing every rescan to full mode, or
    every review-full to the caller's default) fails loudly instead of only
    surfacing as a behavior change no test would catch."""
    doc = _load(_SLASH_COMMANDS_YML)
    job = doc["jobs"]["handle-command"]
    step = _review_step(job, _REVIEW_STEP_NAME)
    forwarded = step.get("with", {})

    expected = (
        "${{ steps.cmd.outputs.command == 'review-full' && 'full' "
        "|| inputs.review-mode-default }}"
    )
    assert forwarded.get("review-mode") == expected, (
        f"{_REVIEW_STEP_NAME!r}'s review-mode expression changed from the expected "
        f"{expected!r} to {forwarded.get('review-mode')!r} -- verify review-full still "
        "forces full mode and rescan still defers to review-mode-default before updating "
        "this pinned value"
    )
