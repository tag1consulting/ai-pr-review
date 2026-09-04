"""Guards against issue #759/#761's bug class: an env var the engine reads
being silently absent from container-action/action.yml's ``docker run -e``
passthrough list, so it's inert on the container path even when the calling
workflow sets it.

This does NOT parse the YAML structurally (no PyYAML dependency needed) --
it regexes the raw file text, scoped to the ``docker run --rm \\`` block only
(covers the main continuation, ``${VAR:+-e VAR}`` conditional forwards, and
the ``SUMMARY_MOUNT`` array literal), for every ``-e VAR`` / ``-e VAR=value``
token. Scoped rather than whole-file so a var name merely mentioned in that
block's own explanatory comments (which name several excluded vars in prose)
can never be mistaken for a forwarded var.

See that file's own comment block (just above ``docker run --rm``) for why
commentary can never be interleaved inside the continuation itself: a ``#``
at a word boundary on a backslash-spliced line starts a comment that runs to
the next real newline, which cuts the command short there. It's not silent --
the truncated ``docker run`` fails immediately (missing its image argument),
and the leftover ``-e VAR \\``-continued lines after the comment are then
parsed as a separate bogus command starting with the literal token ``-e``,
which also fails ("-e: command not found") -- but it's still wrong, so
``test_no_comment_inside_docker_run_continuation`` below asserts it can't
happen again.
"""

import re
from pathlib import Path

from ai_pr_review.config import _KNOWN_AI_VARS

_ACTION_YML_PATH = Path(__file__).resolve().parent.parent.parent / "container-action" / "action.yml"

# AI_* vars that are known to the engine (registered in _KNOWN_AI_VARS) but
# deliberately never forwarded to the container. Each has a documented reason
# in container-action/action.yml's comment block above `docker run --rm`.
_DELIBERATELY_EXCLUDED_AI_VARS = frozenset(
    {
        "AI_AGENT",  # Claude Code's own agent environment, never user-configured.
        "AI_PR_REVIEW_CORRELATION_ID",  # nothing in this action's flow ever sets it.
        "AI_PR_REVIEW_SCRIPT_DIR",  # baked into the image via Dockerfile ENV.
        "AI_PR_REVIEW_RECORD_DIR",  # output path; needs a -v mount this action doesn't provide.
        "AI_PR_REVIEW_COMPUTE_OUTPUT",  # same reason as AI_PR_REVIEW_RECORD_DIR.
        "AI_PR_REVIEW_DIFF_FILE",  # container-internal staging path, same reason.
    }
)

# Non-AI_*-prefixed vars expected in the passthrough list. Unlike AI_* vars,
# these have no central registry (_KNOWN_AI_VARS only covers the AI_ prefix),
# so this is the explicit, hand-maintained source of truth for this test.
#
# LIMITATION: because this allowlist is itself transcribed from the file it
# checks, the two tests built on it (below) can only catch a var being
# accidentally REMOVED from the passthrough list -- they cannot catch a var
# that was never forwarded in the first place (the exact shape of #759/#761
# for a non-AI_*-prefixed var), since there's no independent registry of
# "every non-AI_* var the engine reads" the way _KNOWN_AI_VARS covers AI_*
# ones. If the engine grows a new documented non-AI_* env var, forwarding it
# (or deliberately excluding it, in a comment) is a manual step this test
# cannot force -- add it here explicitly when you do.
_EXPECTED_NON_AI_VARS = frozenset(
    {
        # LLM provider credentials/config
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "BEDROCK_API_KEY",
        "OPENAI_BASE_URL", "BEDROCK_API_URL", "LLM_RETRY_COUNT", "LLM_RETRY_BASE_DELAY",
        "LLM_PROMPT_CACHING",
        # GitHub context (runner-provided or workflow-provided)
        "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS", "GITHUB_REPOSITORY",
        "GITHUB_SERVER_URL", "GITHUB_RUN_ID", "GITHUB_API_URL", "GITHUB_BOT_USERNAME",
        "GITHUB_WORKSPACE", "GITHUB_STEP_SUMMARY",
        # PR/diff identification
        "PR_NUMBER", "BASE_REF", "HEAD_REF", "HEAD_SHA", "REVIEW_TARGET", "MAX_DIFF_LINES",
        "FORCE_FULL_DIFF", "STANDALONE_DEPTH",
        # Analyzer-specific
        "PHPSTAN_LEVEL", "SEMGREP_RULES",
    }
)


def _docker_run_block() -> str:
    """The exact text from ``docker run --rm \\`` through the line containing
    ``"$IMAGE"``, inclusive. Scoping to this region (rather than the whole
    file) keeps this test meaning what its assertions say: "the vars this
    docker run command forwards", not "any var name that appears anywhere in
    this file" (which would also match names mentioned in surrounding prose
    comments, including the deliberately-excluded ones)."""
    text = _ACTION_YML_PATH.read_text()
    match = re.search(r'docker run --rm \\\n(?:.*\n)*?.*"\$IMAGE"', text)
    assert match, "could not find a 'docker run --rm ... \"$IMAGE\"' block in action.yml"
    return match.group(0)


def _forwarded_vars() -> frozenset[str]:
    """Every var name following a literal ``-e`` token in the docker run
    block (covers the main continuation and ``${VAR:+-e VAR}`` conditionals),
    stripping any ``=value`` suffix (e.g. ``-e GITHUB_WORKSPACE=/workspace``
    -> ``GITHUB_WORKSPACE``). Also picks up the ``SUMMARY_MOUNT`` array's
    forwarded var by scanning the whole file for that one specific line,
    since it's built one line above ``docker run --rm`` starts, not inside
    the block itself.
    """
    block = _docker_run_block()
    forwarded = set(re.findall(r"-e\s+([A-Z][A-Z0-9_]*)(?:=\S*)?", block))
    # SUMMARY_MOUNT is built one line above `docker run --rm` starts (its
    # array elements are only spliced into the command later, via
    # "${SUMMARY_MOUNT[@]}"), so it's outside _docker_run_block()'s scope.
    # There are two `SUMMARY_MOUNT=(...)` assignments in the file -- an
    # empty-array default and the conditional one that actually holds `-e
    # GITHUB_STEP_SUMMARY ...` -- so search all of them, not just the first.
    for assignment in re.finditer(r"SUMMARY_MOUNT=\(([^)]*)\)", _ACTION_YML_PATH.read_text()):
        forwarded |= set(re.findall(r"-e\s+([A-Z][A-Z0-9_]*)(?:=\S*)?", assignment.group(1)))
    return frozenset(forwarded)


def test_action_yml_exists() -> None:
    assert _ACTION_YML_PATH.is_file(), f"expected {_ACTION_YML_PATH} to exist"


def test_no_comment_inside_docker_run_continuation() -> None:
    """The regression test for the actual bug hit while writing #761: a `#`
    on a line inside the `docker run \\` backslash-continuation truncates the
    command. Every line in the block (until the one ending the continuation)
    must be either blank, a pure `-e`/`-v`/flag line, or the command's own
    first line -- never a `#`-prefixed comment.
    """
    block = _docker_run_block()
    for line in block.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("#"), (
            f"Found a comment line inside the docker run continuation: {line!r}. "
            f"This truncates the whole command (see this file's module "
            f"docstring, or action.yml's comment block above `docker run --rm`, "
            f"for why). Move it above `docker run --rm \\` instead."
        )


def test_every_known_ai_var_is_forwarded_or_deliberately_excluded() -> None:
    forwarded = _forwarded_vars()
    expected = _KNOWN_AI_VARS - _DELIBERATELY_EXCLUDED_AI_VARS
    missing = expected - forwarded
    assert not missing, (
        f"These AI_* vars are known to the engine (_KNOWN_AI_VARS) but missing "
        f"from container-action/action.yml's docker run -e passthrough list: "
        f"{sorted(missing)}. If genuinely user-facing, add '-e <VAR>' to the "
        f"docker run block. If deliberately internal/unsuitable for the "
        f"container path, add it to _DELIBERATELY_EXCLUDED_AI_VARS here with "
        f"a reason, and document it in action.yml's own comment block."
    )


def test_deliberately_excluded_vars_are_still_known_and_still_absent() -> None:
    """Catches drift in the other direction: an entry in the deny-list that
    either fell out of _KNOWN_AI_VARS (stale) or was actually added to the
    passthrough list without updating this test (the exclusion reason may no
    longer apply)."""
    forwarded = _forwarded_vars()
    stale_denylist_entries = _DELIBERATELY_EXCLUDED_AI_VARS - _KNOWN_AI_VARS
    assert not stale_denylist_entries, (
        f"These vars are in _DELIBERATELY_EXCLUDED_AI_VARS here but no longer "
        f"in ai_pr_review.config._KNOWN_AI_VARS -- remove them from this "
        f"test's denylist: {sorted(stale_denylist_entries)}"
    )
    now_forwarded = _DELIBERATELY_EXCLUDED_AI_VARS & forwarded
    assert not now_forwarded, (
        f"These vars are forwarded in container-action/action.yml but still "
        f"listed as deliberately excluded in this test -- remove them from "
        f"_DELIBERATELY_EXCLUDED_AI_VARS (and check whether the exclusion "
        f"reason still applies before doing so): {sorted(now_forwarded)}"
    )


def test_no_unexpected_ai_var_forwarded() -> None:
    """Every forwarded AI_*-prefixed var must be a real, known var -- catches
    a typo'd '-e AI_FOO' that would otherwise silently do nothing forever."""
    forwarded_ai_vars = {v for v in _forwarded_vars() if v.startswith("AI_")}
    unknown = forwarded_ai_vars - _KNOWN_AI_VARS
    assert not unknown, (
        f"These AI_* vars are forwarded in container-action/action.yml but "
        f"not registered in ai_pr_review.config._KNOWN_AI_VARS -- likely a "
        f"typo, or a new var that needs registering there: {sorted(unknown)}"
    )


def test_expected_non_ai_vars_all_forwarded() -> None:
    forwarded = _forwarded_vars()
    missing = _EXPECTED_NON_AI_VARS - forwarded
    assert not missing, (
        f"These non-AI_* vars are expected in container-action/action.yml's "
        f"docker run -e passthrough list (per this test's _EXPECTED_NON_AI_VARS) "
        f"but are missing: {sorted(missing)}"
    )


def test_no_unexpected_non_ai_var_forwarded() -> None:
    """Catches an accidental removal being masked by this test's own
    allowlist going stale in the other direction, and flags any newly added
    non-AI_* var that should be reviewed and added to _EXPECTED_NON_AI_VARS
    deliberately rather than silently."""
    forwarded = _forwarded_vars()
    non_ai_forwarded = {v for v in forwarded if not v.startswith("AI_")}
    unexpected = non_ai_forwarded - _EXPECTED_NON_AI_VARS
    assert not unexpected, (
        f"These non-AI_* vars are forwarded in container-action/action.yml but "
        f"not in this test's _EXPECTED_NON_AI_VARS allowlist -- if this is a "
        f"deliberate new addition, add it to _EXPECTED_NON_AI_VARS here: "
        f"{sorted(unexpected)}"
    )
