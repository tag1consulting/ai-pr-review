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

``AI_FORK_POLICY`` picks what happens to a gated fork pull request:

* ``label``: the approval status above. This is the safest policy that still reviews, and the
  fallback for an unknown value.
* ``auto`` (opt in): the run starts at once. An author whose ``author_association`` is in
  ``AI_FORK_TRUSTED_ASSOCIATIONS`` gets the normal pipeline. Any other author gets a
  ``ForkProfile`` that restricts the run. The approval status is not read.
* ``block`` (the default): every fork pull request is paused, whoever wrote it.

The status is read from the **base** repository only. A fork owner controls statuses in the
fork, never in the base repository.

Pure logic. The GitHub calls are in ``GitHubProvider.get_fork_gate_facts``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

# The policy constants and the association parser live in ``ai_pr_review.fork_policy``, which has no
# package imports, so ``config.py`` does not import this module. They are re-exported here.
from ai_pr_review.fork_policy import (
    DEFAULT_FORK_MAX_COST_USD,
    DEFAULT_FORK_POLICY,
    DEFAULT_TRUSTED_ASSOCIATIONS,
    FORK_POLICIES,
    SAFEST_FORK_POLICY,
    parse_trusted_associations,
)

__all__ = [
    "DEFAULT_FORK_MAX_COST_USD",
    "DEFAULT_FORK_POLICY",
    "DEFAULT_TRUSTED_ASSOCIATIONS",
    "FORK_POLICIES",
    "SAFEST_FORK_POLICY",
    "parse_trusted_associations",
]

logger = logging.getLogger(__name__)

APPROVAL_CONTEXT = "ai-pr-review/fork-approval"
APPROVAL_LABEL = "ai-review-approve-fork"
MODE_NORMAL, MODE_TRUSTED, MODE_APPROVED, MODE_FORK = "normal", "trusted", "approved", "fork"
UNGATED_EVENTS = frozenset({"pull_request"})
LLM_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "BEDROCK_API_KEY")


@dataclass(frozen=True)
class ForkGateFacts:
    """What the VCS reported about the pull request. ``head_repo`` is empty for a deleted fork."""

    head_repo: str
    base_repo: str
    approval_state: str  # state of the newest status with APPROVAL_CONTEXT on the head commit, or ""
    author_login: str = ""  # "" when the VCS did not report it
    author_association: str = ""  # OWNER, COLLABORATOR, MEMBER, CONTRIBUTOR, ... or "" when not reported


@dataclass(frozen=True)
class ForkProfile:
    """Marks a run as a fork run by a non-trusted author. The budget and spend ledgers, the environment
    scrub, the dropped prompt text and the output clamp read it. Only ``decide`` creates it."""

    head_repo: str
    author_login: str
    author_association: str
    base_repo: str = field(default="", compare=False)  # for the author cap


@dataclass(frozen=True)
class GateDecision:
    gated: bool  # the pull request is a fork under a gated event
    allowed: bool
    reason: str = ""  # shown to maintainers when the run is blocked
    mode: str = MODE_NORMAL  # normal, trusted, approved or fork (see the MODE_ constants)
    profile: ForkProfile | None = None  # set only for mode "fork"


def llm_key_present() -> bool:
    """True when the environment holds any LLM key. Only a name check, the value is never read out."""
    return any(os.environ.get(name, "").strip() for name in LLM_KEY_VARS)

def _minimal_git_env() -> dict[str, str]:
    """The environment of every git launch in this module: no user or system config, no prompt."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    }


def workspace_head(workspace: str = ".") -> str | None:
    """The commit the workspace has checked out, or ``None`` when git cannot say.

    ``rev-parse HEAD`` runs no hook and no filter. The environment is minimal, so no user or
    system config applies. ``safe.directory`` is set on the command line because the container
    runs as another user than the one who owns the checkout.
    """
    env = _minimal_git_env()
    try:
        done = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", workspace, "rev-parse", "HEAD"],
            capture_output=True, text=True, env=env, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("fork gate: could not read the workspace commit (%s)", type(exc).__name__)
        return None
    sha = done.stdout.strip()
    if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    # The git error text names a path at most, never a secret, and is cut short.
    logger.warning(
        "fork gate: git rev-parse HEAD gave exit code %s: %s", done.returncode, (done.stderr or "").strip()[:200]
    )
    return None


_CREDENTIAL_KEYS = r"^(http\.(.*\.)?extraheader|credential\..*|includeif\..*)$"
_REMOTE_URL_KEYS = r"^remote\..*\.(push)?url$"
_INSTEADOF_KEYS = r"^url\..*\.(push)?insteadof$"
_KEY_WITH_USERINFO = re.compile(r"://[^/\s@]+@")
_URL_WITH_USERINFO = re.compile(r"^https?://[^/?#\s]*@", re.IGNORECASE)


def _git_config_regexp(workspace: str, key_regexp: str) -> tuple[int, str]:
    """Run ``git config --local --includes --get-regexp`` in a minimal environment.

    Returns the exit code and stdout. An error or a timeout gives code ``-1``. The environment is
    the one ``workspace_head`` uses, so no user or system config applies and no secret is in reach.
    """
    env = _minimal_git_env()
    try:
        done = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", workspace, "config", "--local", "--includes",
             "--get-regexp", key_regexp],
            capture_output=True, text=True, env=env, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("fork gate: git config could not run (%s)", type(exc).__name__)
        return -1, ""
    if done.returncode not in (0, 1):
        # Exit code 1 only means that no key matched. Anything else is a failure worth a cause.
        logger.warning(
            "fork gate: git config gave exit code %s: %s", done.returncode, (done.stderr or "").strip()[:200]
        )
    return done.returncode, done.stdout


def persisted_credential_reason(workspace: str = ".") -> str:
    """Why a fork run must pause because ``.git/config`` holds a credential, or ``""`` when clean.

    A checkout step that keeps its token in the repository config (an ``http.*.extraheader``, a
    ``credential.*`` helper, an ``includeIf`` that points at a credential file, or a token in a
    remote URL) hands that token to every tool that runs git in the fork's tree. The check fails
    closed: when git cannot answer, the run pauses. The text names keys only, never a value.
    """
    fix = " Set `persist-credentials: false` on the checkout step."
    code, out = _git_config_regexp(workspace, _CREDENTIAL_KEYS)
    if code == 0:
        keys = sorted({line.split(None, 1)[0] for line in out.splitlines() if line.strip()})
        shown = ", ".join(f"`{_redact_key(k)}`" for k in keys[:5])
        return f"The workspace git config holds a persisted credential setting ({shown}), so the review is paused.{fix}"
    if code != 1:
        return "The review is paused because the workspace git config could not be checked for a persisted credential."
    code, out = _git_config_regexp(workspace, _REMOTE_URL_KEYS)
    if code == 0:
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and _URL_WITH_USERINFO.match(parts[1].strip()):
                return f"A remote URL in the workspace git config carries credentials (`{_redact_key(parts[0])}`), so the review is paused.{fix}"
    elif code != 1:
        return "The review is paused because the workspace git config could not be checked for a persisted credential."
    code, out = _git_config_regexp(workspace, _INSTEADOF_KEYS)
    if code == 0:
        for line in out.splitlines():
            parts = line.split(None, 1)
            if parts and _KEY_WITH_USERINFO.search(parts[0]):
                return f"A URL rewrite in the workspace git config carries credentials (`{_redact_key(parts[0])}`), so the review is paused.{fix}"
    elif code != 1:
        return "The review is paused because the workspace git config could not be checked for a persisted credential."
    return ""


def _redact_key(key: str) -> str:
    """Name a config key without echoing its URL part, which can hold ``user:token@host``."""
    if "://" not in key and "@" not in key:
        return key[:80]
    return f"{key.split('.', 1)[0]}.URL.{key.rsplit('.', 1)[-1]}"[:80]


def is_trusted(association: str, trusted: tuple[str, ...]) -> bool:
    """An empty or unknown association is never trusted."""
    return bool(association) and association.strip().upper() in {t.upper() for t in trusted}


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


def _block_text(head_repo: str) -> str:
    source = f"the fork `{head_repo}`" if head_repo else "a fork whose repository is gone"
    return (
        f"This pull request comes from {source}. The repository sets `AI_FORK_POLICY=block`, "
        "so fork pull requests are not reviewed.")


def decide(
    event_name: str,
    head_sha: str,
    facts: ForkGateFacts | None,
    *,
    key_present: bool,
    policy: str = DEFAULT_FORK_POLICY,
    trusted: tuple[str, ...] = DEFAULT_TRUSTED_ASSOCIATIONS,
) -> GateDecision:
    """Decide whether the review may run, and under which mode. ``facts`` is ``None`` when the VCS
    could not be read. An unknown *policy* is treated as ``label`` (``SAFEST_FORK_POLICY``), not as
    the ``block`` default."""
    if not needs_facts(event_name, key_present):
        return GateDecision(gated=False, allowed=True)
    if facts is None:
        return GateDecision(
            gated=True, allowed=False,
            reason="The review is paused because the pull request source could not be checked. "
                   "Re-run the workflow. The gate stays closed when it cannot read the facts.")
    if not is_fork(facts):
        return GateDecision(gated=False, allowed=True)
    if policy == "block":
        return GateDecision(gated=True, allowed=False, reason=_block_text(facts.head_repo))
    if policy == "auto":
        if is_trusted(facts.author_association, trusted):
            return GateDecision(gated=True, allowed=True, mode=MODE_TRUSTED)
        profile = ForkProfile(facts.head_repo, facts.author_login, facts.author_association, facts.base_repo)
        return GateDecision(gated=True, allowed=True, mode=MODE_FORK, profile=profile)
    if facts.approval_state == "success":  # policy "label"
        return GateDecision(gated=True, allowed=True, mode=MODE_APPROVED)
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
