"""Pre-review LLM agents: pr-summarizer and issue-linker.

These agents run before the main review dispatch and produce content that is
prepended to the PR summary comment.  Both are fail-soft: any error is logged
as WARNING and the review continues without their output.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from ai_pr_review.vcs._body import sanitize_display_text

if TYPE_CHECKING:
    from ai_pr_review.llm.base import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

_SUMMARIZER_FAILURE_NOTICE = "> ⚠️ PR summary generation failed — see CI logs.\n\n"


def should_run_summarizer(
    *,
    is_incremental: bool,
    agents: frozenset[str] | tuple[str, ...],
    exclude_agents: frozenset[str] | tuple[str, ...],
) -> bool:
    """Whether pr-summarizer will actually run this review.

    The single source of truth for this gate (#848 item 2): both cli.py's
    real dispatch and review/runtime.py's pre-flight cost estimate must
    agree on when this agent runs, or the estimate silently drifts from
    what actually gets dispatched.
    """
    from ai_pr_review.agents.roster import agent_allowed

    return not is_incremental and agent_allowed("pr-summarizer", agents, exclude_agents)


def should_run_issue_linker(
    *,
    is_incremental: bool,
    review_mode: str,
    vcs_provider: str,
    agents: frozenset[str] | tuple[str, ...],
    exclude_agents: frozenset[str] | tuple[str, ...],
) -> bool:
    """Whether issue-linker will actually run this review. See
    ``should_run_summarizer``'s docstring for why this exists."""
    from ai_pr_review.agents.roster import agent_allowed

    return (
        not is_incremental
        and review_mode == "full"
        and vcs_provider == "github"
        and agent_allowed("issue-linker", agents, exclude_agents)
    )


def preflight_agent_max_tokens(agent_name: str) -> int:
    """Resolve the effective max_output_tokens for a preflight agent
    (pr-summarizer, issue-linker), the same way ``run_summarizer``/
    ``run_issue_linker`` themselves do: the roster's own default as the
    fallback, with any ``AI_MAX_TOKENS_<AGENT>`` override applied on top.

    Deliberately does **not** fall back to ``ReviewConfig.max_tokens_per_agent``
    (the global ``AI_MAX_TOKENS_PER_AGENT`` override) -- that asymmetry is
    real and intentional in ``run_summarizer``/``run_issue_linker`` today,
    not a bug to paper over here; a caller needing the cost estimate to
    mirror actual dispatch must reproduce it, not "fix" it.
    """
    from ai_pr_review.agents.roster import get_agent
    from ai_pr_review.config import resolve_agent_max_tokens

    roster_default = get_agent(agent_name).max_output_tokens
    return resolve_agent_max_tokens(agent_name, roster_default)


async def run_summarizer(
    *,
    diff_text: str,
    manifest_text: str,
    base_ref: str,
    script_dir: Path,
    model: str,
    temperature: float = 0.3,
    llm_call: Callable[[LLMRequest], Awaitable[LLMResponse]],
    collapse_walkthrough: bool = False,
) -> str:
    """Run the pr-summarizer agent and return its formatted markdown output.

    Returns response.text (the full LLM markdown including summary and
    walkthrough table) rather than SummarizerOutput.summary_md, because
    summary_md contains only the text before the **Type:** line — returning it
    alone would drop the walkthrough table.
    parse_summarizer_output() is called for validation (and, when
    collapse_walkthrough is set, to get the file count for free — its
    typed WalkthroughRow tuple is otherwise discarded).

    collapse_walkthrough: when True, wraps the `## Walkthrough` section in a
    collapsed `<details>` accordion via `wrap_walkthrough_in_details`. Callers
    must gate this on the VCS provider — Bitbucket Cloud renders no HTML at
    all (issue #703), so it must stay False there. See cli.py's provider gate.

    Fail-soft: on any error logs a WARNING and returns _SUMMARIZER_FAILURE_NOTICE
    so the PR comment communicates the partial failure rather than silently
    omitting the summary.  except Exception is intentional: the fail-soft contract
    requires that any unexpected error (KeyError, TypeError, etc.) in the prompt
    assembly or parse path skips the summary rather than aborting the whole review.
    """
    from ai_pr_review.agents.roster import get_agent
    from ai_pr_review.agents.summarizer import (
        build_summarizer_system_prompt,
        build_summarizer_user_message,
        parse_summarizer_output,
        wrap_walkthrough_in_details,
    )
    from ai_pr_review.llm.base import LLMRequest

    try:
        prompt_path = script_dir / "prompts" / "pr-summarizer.md"
        system_prompt = build_summarizer_system_prompt(prompt_path)

        commit_log = ""
        _git_cmd = ["git", "log", "--format=%h %s%n%b", "--max-count=20", f"origin/{base_ref}..HEAD"]
        proc: subprocess.CompletedProcess[str] | None = None
        try:
            proc = await anyio.to_thread.run_sync(
                lambda: subprocess.run(
                    _git_cmd, capture_output=True, text=True, timeout=15,
                )
            )
        except subprocess.TimeoutExpired:
            logger.warning("pr-summarizer: git log timed out; proceeding without commit log")
        except Exception as exc:
            logger.warning("pr-summarizer: could not get commit log: %s", exc)
        else:
            if proc.returncode != 0:
                logger.warning(
                    "pr-summarizer: git log exited %d; stderr=%r stdout=%r",
                    proc.returncode, proc.stderr.strip()[:500], proc.stdout.strip()[:500],
                )
                commit_log = "_Note: commit log unavailable (git log failed)._"
            else:
                commit_log = proc.stdout.strip()

        user_message = build_summarizer_user_message(manifest_text, commit_log, diff_text)
        # #191/#847/#848: resolves the default from the roster itself (via
        # preflight_agent_max_tokens, the same helper review/runtime.py's
        # cost estimate now calls for this agent) rather than a hand-typed
        # literal or an inline duplicate of this resolution, so estimate
        # and actual dispatch can never drift apart.
        roster_default = get_agent("pr-summarizer").max_output_tokens
        max_tokens = preflight_agent_max_tokens("pr-summarizer")
        logger.debug(
            "pr-summarizer: effective max_output_tokens=%d (source=%s)",
            max_tokens, "per-agent override" if max_tokens != roster_default else "roster default",
        )
        request = LLMRequest(
            model_id=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        response: LLMResponse = await llm_call(request)
        logger.debug("pr-summarizer: raw response length=%d chars", len(response.text))
        # #886: the full response is carried forward verbatim into every
        # subsequent review's summary comment, which marker.py's extract_*
        # functions (id-map, verdicts, summary SHA) later re-read on the
        # next run -- sanitize before parsing/returning so LLM-derived text
        # (steerable via prompt injection in the diff/PR description) can't
        # contain a syntactically valid forged marker. parse_summarizer_output
        # runs on the sanitized text too; its Type:/Effort:/table parsing
        # doesn't rely on any of the defanged sequences.
        text = sanitize_display_text(response.text)
        parsed = parse_summarizer_output(text)
        if collapse_walkthrough:
            return wrap_walkthrough_in_details(text, file_count=len(parsed.walkthrough))
        return text
    except Exception as exc:
        logger.warning(
            "pr-summarizer: failed (review will continue without summary): %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        return _SUMMARIZER_FAILURE_NOTICE


def fetch_open_issues(github_repository: str) -> str:
    """Fetch open issues via gh CLI and return a compact text block, or "(unavailable)".

    Runs ``gh issue list`` synchronously (call via anyio.to_thread.run_sync from async
    context).  Always returns a string; never raises.  Returns "(unavailable)" on any
    error so that the caller can still pass a user message to the LLM.

    Each line in the returned block has the format::

        #<number> <title> [label1, label2]
    """
    import json as _json

    cmd = [
        "gh", "issue", "list",
        "--repo", github_repository,
        "--state", "open",
        "--limit", "50",
        "--json", "number,title,labels",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        logger.warning("issue-linker: gh CLI not found; open-issue list unavailable")
        return "(unavailable)"
    except subprocess.TimeoutExpired:
        logger.warning("issue-linker: gh issue list timed out; open-issue list unavailable")
        return "(unavailable)"
    except Exception as exc:
        logger.warning("issue-linker: gh issue list failed: %s", exc, exc_info=True)
        return "(unavailable)"

    if proc.returncode != 0:
        stderr_snippet = proc.stderr.strip()[:500]
        truncated = "…" if len(proc.stderr.strip()) > 500 else ""
        logger.warning(
            "issue-linker: gh issue list exited %d; open-issue list unavailable: %s%s",
            proc.returncode, stderr_snippet, truncated,
        )
        return "(unavailable)"

    try:
        issues = _json.loads(proc.stdout)
    except _json.JSONDecodeError as exc:
        logger.warning("issue-linker: could not parse gh issue list output: %s", exc)
        return "(unavailable)"

    if not issues:
        return "(no open issues)"

    lines: list[str] = []
    for item in issues:
        number = item.get("number", "?")
        title = item.get("title", "(no title)")
        label_names = [lb.get("name", "") for lb in item.get("labels", []) if lb.get("name")]
        label_str = f" [{', '.join(label_names)}]" if label_names else ""
        lines.append(f"#{number} {title}{label_str}")
    return "\n".join(lines)


async def run_issue_linker(
    *,
    manifest_text: str,
    base_ref: str,
    script_dir: Path,
    provider: str,
    github_repository: str,
    model: str,
    temperature: float = 0.3,
    llm_call: Callable[[LLMRequest], Awaitable[LLMResponse]],
) -> str:
    """Run the issue-linker agent and return its markdown output, or "" to suppress.

    ``provider`` is the VCS provider (github/bitbucket/gitlab), NOT the AI provider.
    The issue-linker is GitHub-only. When the provider is not github or the agent
    returns the sentinel NONE, this function returns "" so the caller skips
    appending to summary_text. Any error is logged as WARNING and "" is returned
    (fail-soft).

    The user message provides the context the agent needs: commit log, branch name,
    open issues (pre-fetched deterministically via ``gh issue list``), file manifest,
    repository slug, and PROVIDER. The agent itself executes no shell commands or tools;
    the open-issue list is injected as plain text so the model can match and cite real
    issue numbers without any tool-calling loop.
    """
    from ai_pr_review.agents.roster import get_agent
    from ai_pr_review.llm.base import LLMRequest

    try:
        prompt_path = script_dir / "prompts" / "issue-linker.md"
        if not prompt_path.exists():
            logger.warning("issue-linker: prompt not found at %s; skipping", prompt_path)
            return ""
        system_prompt = prompt_path.read_text()

        commit_log = ""
        _git_cmd = ["git", "log", "--format=%h %s%n%b", "--max-count=20", f"origin/{base_ref}..HEAD"]

        def _run_git_log() -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(_git_cmd, capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                return None

        try:
            proc = await anyio.to_thread.run_sync(_run_git_log)
        except Exception as exc:
            logger.warning("issue-linker: could not get commit log: %s", exc, exc_info=True)
        else:
            if proc is None:
                logger.warning("issue-linker: git log timed out; proceeding without commit log")
            elif proc.returncode == 0:
                commit_log = proc.stdout.strip()
            else:
                commit_log = "_Note: commit log unavailable (git log failed)._"

        branch_name = ""

        def _run_git_branch() -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                return None

        try:
            branch_proc = await anyio.to_thread.run_sync(_run_git_branch)
        except Exception as exc:
            logger.warning("issue-linker: could not get branch name: %s", exc, exc_info=True)
        else:
            if branch_proc is None:
                logger.warning("issue-linker: git rev-parse timed out; proceeding without branch name")
            elif branch_proc.returncode == 0:
                branch_name = branch_proc.stdout.strip()

        if not commit_log and not branch_name:
            logger.warning(
                "issue-linker: both commit log and branch name unavailable "
                "(not a git repo, or git is absent); skipping"
            )
            return ""

        open_issues_text = await anyio.to_thread.run_sync(
            lambda: fetch_open_issues(github_repository)
        )

        user_message = (
            f"PROVIDER: {provider}\n\n"
            f"REPOSITORY: {github_repository}\n\n"
            f"## Branch Name\n\n{branch_name or '(unavailable)'}\n\n"
            f"## Commit Log\n\n{commit_log or '(unavailable)'}\n\n"
            f"## Open Issues\n\n{open_issues_text}\n\n"
            f"## File Manifest\n\n{manifest_text}\n"
        )

        # #191/#847/#848: same rationale as pr-summarizer above -- resolves
        # via preflight_agent_max_tokens, the same helper review/runtime.py's
        # cost estimate calls for this agent, so estimate and actual
        # dispatch can never drift apart.
        roster_default = get_agent("issue-linker").max_output_tokens
        max_tokens = preflight_agent_max_tokens("issue-linker")
        logger.debug(
            "issue-linker: effective max_output_tokens=%d (source=%s)",
            max_tokens, "per-agent override" if max_tokens != roster_default else "roster default",
        )
        request = LLMRequest(
            model_id=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        response: LLMResponse = await llm_call(request)
        text = response.text.strip()
        if text == "NONE" or not text:
            logger.debug("issue-linker: returned NONE or empty; skipping")
            return ""
        # #886: same rationale as run_summarizer above -- this output is
        # also carried forward verbatim into the posted summary comment.
        return sanitize_display_text(text)
    except Exception as exc:
        logger.warning(
            "issue-linker: failed (review will continue without issue links): %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        return ""

