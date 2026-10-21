"""Fork pull request gate (#1088 follow-up, container split option 3a).

A workflow that runs on ``pull_request_target`` holds the repository's LLM keys and a write
token, and it checks out the pull request's head. For a fork, that head is code anyone can
write. This module decides, before the engine runs any git command in the fork tree, whether a
fork pull request may be reviewed at all.

The rule, kept deliberately small:

* A plain ``pull_request`` run is gated only when an LLM key is present in the environment.
  GitHub withholds secrets from a fork on that event by default, so no key means nothing to
  protect and the run could not call a model anyway. A private repository can turn on "send
  secrets to workflows from fork pull requests", and then a key is present and the gate applies.
* Any other event (``pull_request_target``, ``issue_comment``, an unknown event) is gated when
  the head repository is not the base repository. A deleted head repository counts as a fork.
* A gated pull request is reviewed only when the base repository holds a successful commit
  status named ``ai-pr-review/fork-approval`` on the exact head commit. A trusted workflow
  creates that status after a maintainer approves (see ``examples/workflows/fork-approval.yml``).
  A push gives a new commit, and the new commit has no status, so an approval never carries
  over to code the maintainer did not see.
* The checked-out tree must be the approved commit. The approval names ``HEAD_SHA``, but the
  analyzers read the working tree, so ``git rev-parse HEAD`` of the workspace must equal it.
  A workflow that checks out a branch name could otherwise review code newer than the approval.
* If the facts cannot be read, the run is blocked. The gate fails closed.

The status is read from the **base** repository only. A fork owner controls statuses in the
fork, never in the base repository.

Pure logic. The GitHub calls are in ``GitHubProvider.get_fork_gate_facts``.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

APPROVAL_CONTEXT = "ai-pr-review/fork-approval"
APPROVAL_LABEL = "ai-review-approve-fork"
DEFAULT_FORK_MAX_COST_USD = 2.0
UNGATED_EVENTS = frozenset({"pull_request"})
LLM_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "BEDROCK_API_KEY")


@dataclass(frozen=True)
class ForkGateFacts:
    """What the VCS reported about the pull request. ``head_repo`` is empty for a deleted fork."""

    head_repo: str
    base_repo: str
    approval_state: str  # state of the newest status with APPROVAL_CONTEXT on the head commit, or ""


@dataclass(frozen=True)
class GateDecision:
    gated: bool  # the pull request is a fork under a gated event
    allowed: bool
    reason: str = ""  # shown to maintainers when the run is blocked


def llm_key_present() -> bool:
    """True when the environment holds any LLM key. Only a name check, the value is never read out."""
    return any(os.environ.get(name, "").strip() for name in LLM_KEY_VARS)


def workspace_head(workspace: str = ".") -> str | None:
    """The commit the workspace has checked out, or ``None`` when git cannot say.

    ``rev-parse HEAD`` runs no hook and no filter. The environment is minimal, so no user or
    system config applies. ``safe.directory`` is set on the command line because the container
    runs as another user than the one who owns the checkout.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        done = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", workspace, "rev-parse", "HEAD"],
            capture_output=True, text=True, env=env, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def is_fork(facts: ForkGateFacts) -> bool:
    return not facts.head_repo or facts.head_repo.casefold() != facts.base_repo.casefold()


def needs_facts(event_name: str, key_present: bool) -> bool:
    """False only for a plain ``pull_request`` run that holds no key, which needs no API call."""
    return not (event_name in UNGATED_EVENTS and not key_present)


def _blocked_text(head_sha: str, head_repo: str, state: str) -> str:
    short = head_sha[:7] or "this commit"
    source = f"the fork `{head_repo}`" if head_repo else "a fork whose repository is gone"
    state_note = (
        " An approval exists for this commit but it is not in a successful state." if state and state != "success"
        else ""
    )
    return (
        f"This pull request comes from {source}, so the review is paused. A maintainer with write access "
        f"must read the full pull request diff (Files changed) at commit `{short}` and then add the "
        f"`{APPROVAL_LABEL}` label. The approval covers commit `{short}` only. A new push needs a new "
        f"approval.{state_note}"
    )


def decide(event_name: str, head_sha: str, facts: ForkGateFacts | None, *, key_present: bool) -> GateDecision:
    """Decide whether the review may run. ``facts`` is ``None`` when the VCS could not be read."""
    if not needs_facts(event_name, key_present):
        return GateDecision(gated=False, allowed=True)
    if facts is None:
        return GateDecision(
            gated=True, allowed=False,
            reason="The review is paused because the pull request source could not be checked. "
                   "Re-run the workflow. The gate stays closed when it cannot read the facts.")
    if not is_fork(facts):
        return GateDecision(gated=False, allowed=True)
    if facts.approval_state == "success":
        return GateDecision(gated=True, allowed=True)
    return GateDecision(gated=True, allowed=False, reason=_blocked_text(head_sha, facts.head_repo, facts.approval_state))


def checkout_mismatch_reason(head_sha: str, checked_out: str | None) -> str:
    """The skip text when the workspace is not at the approved commit. Empty when it matches."""
    if checked_out == head_sha:
        return ""
    found = checked_out[:7] if checked_out else "an unknown commit"
    return (
        f"This fork pull request was approved at commit `{head_sha[:7]}`, but the workspace holds {found}. "
        "The review is paused. Check out `github.event.pull_request.head.sha` in the workflow, "
        "never a branch name.")
