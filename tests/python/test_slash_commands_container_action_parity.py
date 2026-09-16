"""Guards against issue #863's bug class: a `workflow_call` input on
`.github/workflows/slash-commands.yml` that mirrors a `container-action`
input has no effect unless it is actually forwarded to `container-action`
from both review-dispatch steps, with a default matching `container-action`'s
own. `max-diff-lines`/`max-inline`/`ignore-merge-commits`/`context-enrichment`
existed nowhere in slash-commands.yml at all before #863; `analyzers`/
`exclude-analyzers`/`agents`/`exclude-agents` were fixed the same way earlier
by issue #516. This test makes a future instance of the same drift (a new
review-tuning knob added to one side but not the other, or a default that
falls out of sync) a loud CI failure instead of a silent one.

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

_REVIEW_STEP_NAMES = ("Run review (rescan)", "Run review (review-full)")

# The review-tuning inputs slash-commands.yml passes straight through to
# container-action under the same name on both sides. This is a maintained
# allowlist, not "every name the two files share" -- inputs like
# `image-tag`/`provider`/`base-url`/`github-token`/`pr-number`/`base-ref`/
# `head-ref`/`head-sha` are GitHub-context or connection plumbing (covered by
# test_slash_commands_workflow.py and hand-verified elsewhere), not review
# behavior knobs. Adding a new review-tuning knob to both files should add it
# here too.
_REVIEW_TUNING_INPUTS = frozenset(
    {
        "max-diff-lines",
        "max-inline",
        "ignore-merge-commits",
        "context-enrichment",
        "analyzers",
        "exclude-analyzers",
        "agents",
        "exclude-agents",
    }
)


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


def test_review_tuning_inputs_are_declared_on_both_sides() -> None:
    workflow_inputs = _workflow_call_inputs()
    container_inputs = _container_action_inputs()

    missing_from_workflow = _REVIEW_TUNING_INPUTS - set(workflow_inputs)
    assert not missing_from_workflow, (
        f"{sorted(missing_from_workflow)} listed in _REVIEW_TUNING_INPUTS but not declared as a "
        f"workflow_call input in {_SLASH_COMMANDS_YML}"
    )

    missing_from_container = _REVIEW_TUNING_INPUTS - set(container_inputs)
    assert not missing_from_container, (
        f"{sorted(missing_from_container)} listed in _REVIEW_TUNING_INPUTS but not declared in "
        f"{_CONTAINER_ACTION_YML} -- update the allowlist if these no longer apply"
    )


def test_review_tuning_inputs_are_forwarded_in_both_review_steps() -> None:
    doc = _load(_SLASH_COMMANDS_YML)
    job = doc["jobs"]["handle-command"]

    for step_name in _REVIEW_STEP_NAMES:
        step = _review_step(job, step_name)
        forwarded = step.get("with", {})
        for name in sorted(_REVIEW_TUNING_INPUTS):
            expected = f"${{{{ inputs.{name} }}}}"
            assert forwarded.get(name) == expected, (
                f"{step_name!r} does not forward `{name}` to container-action as {expected!r} "
                f"(got {forwarded.get(name)!r}) -- a workflow_call input with no effect on the "
                "actual review is exactly issue #863's bug class"
            )


def test_review_tuning_input_defaults_match_container_action() -> None:
    workflow_inputs = _workflow_call_inputs()
    container_inputs = _container_action_inputs()

    for name in sorted(_REVIEW_TUNING_INPUTS):
        workflow_default = workflow_inputs[name].get("default")
        container_default = container_inputs[name].get("default")
        assert workflow_default == container_default, (
            f"`{name}` defaults to {workflow_default!r} in {_SLASH_COMMANDS_YML} but "
            f"{container_default!r} in {_CONTAINER_ACTION_YML} -- omitting this input from a "
            "caller workflow would silently change behavior instead of being a no-op"
        )
