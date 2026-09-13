"""Guards against action.yml / container-action/action.yml input drift
(issue #821).

The two files describe the same review-engine interface twice -- once for
the direct composite-action path (`action.yml`) and once for the
containerized path (`container-action/action.yml`) -- and are maintained by
hand as separate YAML files. A new input added to one silently never reaches
the other unless someone remembers to update both; this test makes that
drift a loud CI failure instead.

Structural (`yaml.safe_load`), unlike test_container_action_env.py's
regex-scoped approach: both files' `inputs:` blocks are plain YAML mappings
with no shell/bash content to accidentally match, so there's no risk of a
regex being fooled by a name mentioned in prose.

Written while implementing #821, this test's first real run caught a live,
pre-existing gap: `context-max-queries` and `fail-on-findings` existed in
container-action/action.yml but not in action.yml at all (no input, no env
var wired to the composite step) -- a consumer of the direct composite
action had no way to set either. Fixed as part of this same change (both
inputs added to action.yml, wired to AI_CONTEXT_MAX_QUERIES/AI_FAIL_ON_FINDINGS
in its `runs:` step, matching container-action's semantics). `image-tag` and
`registry-token` remain container-only deliberately -- see
_CONTAINER_ONLY_INPUTS below.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT_ACTION_YML = Path(__file__).resolve().parents[2] / "action.yml"
_CONTAINER_ACTION_YML = Path(__file__).resolve().parents[2] / "container-action" / "action.yml"

# Inputs that exist ONLY in container-action/action.yml, deliberately: both
# are properties of pulling and running a pinned container image, which the
# direct composite-action path (action.yml) has no equivalent concept for at
# all -- there's no image to tag or authenticate against.
_CONTAINER_ONLY_INPUTS = frozenset({"image-tag", "registry-token"})


def _input_keys(path: Path) -> set[str]:
    doc = yaml.safe_load(path.read_text())
    inputs = doc.get("inputs")
    assert isinstance(inputs, dict), f"{path} has no top-level 'inputs:' mapping"
    return set(inputs.keys())


def test_both_action_files_exist() -> None:
    assert _ROOT_ACTION_YML.is_file(), f"expected {_ROOT_ACTION_YML} to exist"
    assert _CONTAINER_ACTION_YML.is_file(), f"expected {_CONTAINER_ACTION_YML} to exist"


def test_no_input_is_missing_from_either_file() -> None:
    root_inputs = _input_keys(_ROOT_ACTION_YML)
    container_inputs = _input_keys(_CONTAINER_ACTION_YML)

    missing_from_root = container_inputs - root_inputs - _CONTAINER_ONLY_INPUTS
    assert not missing_from_root, (
        f"container-action/action.yml declares {sorted(missing_from_root)} but "
        "action.yml does not. If this is a real engine-wide input, add it to "
        "action.yml (and wire it to the matching AI_* env var in its `runs:` "
        "step) too. If it's genuinely container-only, add it to "
        "_CONTAINER_ONLY_INPUTS here with a reason."
    )

    missing_from_container = root_inputs - container_inputs
    assert not missing_from_container, (
        f"action.yml declares {sorted(missing_from_container)} but "
        "container-action/action.yml does not -- add it there too (and wire "
        "it to the matching AI_* env var / docker run -e passthrough)."
    )


def test_container_only_allowlist_entries_are_still_container_only() -> None:
    """Catches drift in the other direction: an allowlist entry that was
    actually added to action.yml without updating this test (in which case
    the exclusion is now unnecessary and should be removed), or one that
    disappeared from container-action/action.yml entirely (stale entry)."""
    root_inputs = _input_keys(_ROOT_ACTION_YML)
    container_inputs = _input_keys(_CONTAINER_ACTION_YML)

    now_in_both = _CONTAINER_ONLY_INPUTS & root_inputs
    assert not now_in_both, (
        f"These inputs are in _CONTAINER_ONLY_INPUTS here but now exist in "
        f"action.yml too -- remove them from _CONTAINER_ONLY_INPUTS: {sorted(now_in_both)}"
    )

    stale_entries = _CONTAINER_ONLY_INPUTS - container_inputs
    assert not stale_entries, (
        f"These inputs are in _CONTAINER_ONLY_INPUTS here but no longer in "
        f"container-action/action.yml -- remove them from this test's "
        f"allowlist: {sorted(stale_entries)}"
    )
