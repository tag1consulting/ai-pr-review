"""Fork pull request gate (#1088 follow-up, container split option 3a).

A workflow that runs on ``pull_request_target`` holds the repository's LLM keys and a write
token, and it checks out the pull request's head. For a fork, that head is code anyone can
write. This module decides, before the engine runs any git command in the fork tree, whether a
fork pull request may be reviewed at all.

The rule, kept deliberately small:

* A plain ``pull_request`` run is never gated here. GitHub withholds the secrets from a fork
  on that event, so there is nothing to protect.
* Any other event (``pull_request_target``, ``issue_comment``, an unknown event) is gated when
  the head repository is not the base repository. A deleted head repository counts as a fork.
* A gated pull request is reviewed only when the base repository holds a successful commit
  status named ``ai-pr-review/fork-approval`` on the exact head commit. A trusted workflow
  creates that status after a maintainer approves (see ``examples/workflows/fork-approval.yml``).
  A push gives a new commit, and the new commit has no status, so an approval never carries
  over to code the maintainer did not see.
* If the facts cannot be read, the run is blocked. The gate fails closed.

The status is read from the **base** repository only. A fork owner controls statuses in the
fork, never in the base repository.

Pure logic. The GitHub calls are in ``GitHubProvider.get_fork_gate_facts``.
"""

from __future__ import annotations

from dataclasses import dataclass

APPROVAL_CONTEXT = "ai-pr-review/fork-approval"
APPROVAL_LABEL = "ai-review-approve-fork"
DEFAULT_FORK_MAX_COST_USD = 2.0
UNGATED_EVENTS = frozenset({"pull_request"})


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


def is_fork(facts: ForkGateFacts) -> bool:
    return not facts.head_repo or facts.head_repo.casefold() != facts.base_repo.casefold()


def _blocked_text(head_sha: str, head_repo: str, state: str) -> str:
    short = head_sha[:7] or "this commit"
    source = f"the fork `{head_repo}`" if head_repo else "a fork whose repository is gone"
    state_note = (
        " An approval exists for this commit but it is not in a successful state." if state and state != "success"
        else ""
    )
    return (
        f"This pull request comes from {source}, so the review is paused. A maintainer with write access "
        f"must approve commit `{short}` by adding the `{APPROVAL_LABEL}` label. The approval covers that "
        f"commit only. A new push needs a new approval.{state_note}"
    )


def decide(event_name: str, head_sha: str, facts: ForkGateFacts | None) -> GateDecision:
    """Decide whether the review may run. ``facts`` is ``None`` when the VCS could not be read."""
    if event_name in UNGATED_EVENTS:
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
