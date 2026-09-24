"""Feedback store — E3.S8 (GitBranchStore + protocol).

ADR-0001: feedback is persisted as a JSONL file on a dedicated git branch
(``ai-pr-review-bot`` by default) so it survives PR branch deletion and
repository forks.

``FeedbackStore`` is the protocol all store implementations satisfy.
``GitBranchStore`` is the GitHub-backed implementation.
``UnsupportedVcsStore`` is a no-op stub for GitLab / Bitbucket.

Concurrency model: optimistic-lock via ETag / SHA-based if-match.  On a
conflict (HTTP 409 or SHA mismatch) the store retries up to ``_MAX_RETRIES``
times with random jitter before giving up (fail-soft: the review still posts,
feedback is silently dropped with a WARNING log).
"""

from __future__ import annotations

import base64
import datetime
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.feedback.retention import _parse_ts, apply_retention

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_S = 0.5
_STORE_PATH = ".ai-pr-review/learnings.jsonl"

# Issue #769: the append path has no dedup at all, so two independent callers
# writing the same verdict seconds apart (the double-write bug this window
# guards against) produced two identical JSONL lines differing only in `ts`.
# A short window rather than "ever seen this key" is deliberate: re-dismissing
# the same finding weeks later on a genuinely new run is real signal and must
# not be silently absorbed, whereas two writes seconds apart are a bug. Loud
# by design (WARNING + ::warning::) rather than a silent skip, so a future
# recurrence of the underlying double-write bug still surfaces somewhere.
_DEDUP_WINDOW = datetime.timedelta(minutes=10)

# Only the commands issue #769's double-write bug actually applied to.
# Deliberately excludes bare `feedback` (free-form text notes with no
# structural identity beyond `reason`, which _dedup_key excludes by design --
# two distinct feedback notes posted minutes apart with no file/source
# context would otherwise collide on the same key and the second would be
# silently dropped, a real regression two independent reviewers of this PR
# flagged) and `fixed` (never reaches the store at all -- see
# DismissResult.feedback_eligible). Narrowing to this set means the dedup
# guard can only ever fire for the bug class it exists to catch.
_DEDUP_ELIGIBLE_COMMANDS = frozenset({"false-positive", "wont-fix"})


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class FeedbackStore(Protocol):
    """Read/write interface for the feedback store."""

    def append(self, entry: FeedbackEntry) -> bool:
        """Append one entry.  Fail-soft: log WARNING on error.

        Returns ``True`` if the entry was successfully persisted, ``False``
        otherwise.  Callers (e.g. ``slash/handlers.py``) use this to decide
        whether to acknowledge the user's command with "recorded" or
        "could not persist".
        """
        ...

    def load_recent(self) -> list[FeedbackEntry]:
        """Return all stored entries (newest first).  Return [] on error."""
        ...


# ---------------------------------------------------------------------------
# UnsupportedVcsStore — stub for GitLab / Bitbucket
# ---------------------------------------------------------------------------

class UnsupportedVcsStore:
    """No-op store for VCS providers where feedback loop is not yet implemented."""

    def append(self, entry: FeedbackEntry) -> bool:
        logger.info("feedback store: VCS provider not supported; entry dropped")
        return False

    def load_recent(self) -> list[FeedbackEntry]:
        return []


def _dedup_key(entry: FeedbackEntry) -> tuple[str, str, str, str, object]:
    """Identity used to detect a duplicate append (issue #769).

    `reason` is deliberately excluded: the two writers that produced this
    bug's duplicate pairs (`dismiss`/`dismiss-inline` vs. the now-removed
    `feedback-command` `slash` write) rendered different reason text for the
    same verdict, so keying on `reason` would have let the exact bug this
    guards against slip through.
    """
    return (entry.command, entry.source, entry.file, entry.rule_id, entry.extras.get("finding_id"))


def _find_recent_duplicate(
    entry: FeedbackEntry, existing_entries: list[FeedbackEntry]
) -> FeedbackEntry | None:
    """Return the existing entry `entry` duplicates within `_DEDUP_WINDOW`, if any.

    Only considers `entry.command in _DEDUP_ELIGIBLE_COMMANDS` -- see that
    constant's docstring for why this must stay narrow.

    `existing_entries` is newest-first (as returned by `_parse_jsonl`), but
    that ordering is not scanned as a hard invariant: this walks the full
    list rather than breaking on the first out-of-window entry, since a
    manually edited file or a genuine clock-skew race between two writers
    (the exact scenario issue #769 exists because of) could put an
    out-of-order entry ahead of the true in-window duplicate, and an early
    break would silently miss it -- undercutting this guard's own "a
    recurrence still surfaces" purpose. Bounded by retention_count regardless
    (500 by default), so the extra scan cost is negligible.
    """
    if entry.command not in _DEDUP_ELIGIBLE_COMMANDS:
        return None
    new_dt = _parse_ts(entry.ts)
    if new_dt is None:
        return None
    key = _dedup_key(entry)
    for existing in existing_entries:
        if existing.command not in _DEDUP_ELIGIBLE_COMMANDS:
            continue
        existing_dt = _parse_ts(existing.ts)
        if existing_dt is None:
            continue
        if abs(new_dt - existing_dt) > _DEDUP_WINDOW:
            continue
        if _dedup_key(existing) == key:
            return existing
    return None


# ---------------------------------------------------------------------------
# _StoreBackend: provider-neutral read/write primitives (issue #906 PR 2/3)
# ---------------------------------------------------------------------------
#
# Extracted from what was, before this, all inline on GitBranchStore. The
# split exists so a second backend (Bitbucket's `/src` endpoint, PR 3/3) can
# reuse the retry/jitter, #769 dedup, retention, and oldest-first JSONL wire
# format below (`_StoreCore`) instead of re-deriving them. `GitBranchStore`
# keeps its public name/constructor and every private method
# `tests/python/test_feedback_store.py` calls directly
# (`_fetch_file_meta`/`_branch_exists`/`_append_once`/`_parse_jsonl`) as thin
# delegating wrappers, so that suite is unchanged by this refactor.

class _StoreBackend(Protocol):
    """Raw read/write primitives one VCS provider's feedback-store backend
    supplies. `_StoreCore` is provider-neutral and depends only on this."""

    def read(self) -> tuple[str | None, object | None]:
        """Return (decoded text content, version token), or (None, None) if
        the file doesn't exist. May raise ``RuntimeError`` if the file is too
        large to safely round-trip (see `_GitHubContentsBackend.read`)."""
        ...

    def write(self, content: str, version: object | None) -> None:
        """Write *content*, using *version* for an optimistic-lock compare
        (``None`` when creating fresh / no prior version is known). Raises
        ``_ConflictError`` on a lock conflict, ``_MissingBranchError`` if the
        target branch doesn't exist."""
        ...

    def branch_exists(self) -> bool | None:
        """Tri-state: True/False/None (None means unknown, see `_StoreCore.append`)."""
        ...

    def bootstrap_branch(self) -> bool:
        """Create the target branch from the repo's default branch HEAD."""
        ...


@dataclass
class _StoreCore:
    """Provider-neutral store logic: retry/jitter, issue #769 dedup,
    retention, oldest-first JSONL wire format, fail-soft error handling.
    Operates on a `_StoreBackend` so GitHub (`_GitHubContentsBackend`) and
    Bitbucket (`_BitbucketSrcBackend`, PR 3/3) share this instead of each
    re-deriving it.
    """

    backend: _StoreBackend
    retention_count: int = 500
    retention_age_days: int = 365

    def append(self, entry: FeedbackEntry) -> bool:
        """Append *entry* with optimistic-lock retry.

        On a missing-branch signal, attempts to create the feedback branch
        from the default branch's HEAD and retries the write.

        Returns True if the entry was persisted; False on any failure mode
        (network error, exhausted retries, missing branch that couldn't be
        bootstrapped, unexpected exception).
        """
        bootstrap_attempted = False
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._append_once(entry)
                return True
            except _MissingBranchError as exc:
                # Branch doesn't exist — create it from the default branch
                # and retry once. Avoid infinite loop if bootstrap also fails.
                if bootstrap_attempted:
                    logger.warning(
                        "feedback store: branch %r still missing after bootstrap; entry dropped",
                        exc.branch,
                    )
                    return False
                bootstrap_attempted = True
                if not self.backend.bootstrap_branch():
                    return False
                # Loop back without incrementing the conflict retry budget
                continue
            except _ConflictError:
                if attempt == _MAX_RETRIES:
                    logger.warning(
                        "[ai-pr-review] WARNING: feedback store: SHA conflict after %d attempts; entry dropped",
                        _MAX_RETRIES,
                    )
                    return False
                jitter = random.uniform(0, _RETRY_BASE_S * attempt)
                time.sleep(_RETRY_BASE_S * attempt + jitter)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: feedback store: HTTP error on append attempt %d: %s",
                    attempt, exc,
                )
                return False
            except RuntimeError as exc:
                # backend.read() raises RuntimeError for files >1 MB.
                # Catch it explicitly so the WARNING mentions the actual cause
                # rather than masquerading as a generic "unexpected error".
                logger.warning(
                    "feedback store: cannot append — %s. "
                    "Consider lowering AI_FEEDBACK_RETENTION_COUNT or trimming "
                    "the feedback file manually.",
                    exc,
                )
                return False
            except Exception:
                logger.error(
                    "[ai-pr-review] ERROR: feedback store: unexpected error in append (entry dropped)",
                    exc_info=True,
                )
                return False
        return False

    def load_recent(self) -> list[FeedbackEntry]:
        """Return all entries (newest-first).  Return [] on any error."""
        try:
            content, _version = self.backend.read()
            if content is None:
                return []
            return self._parse_jsonl(content)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            logger.warning("feedback store: HTTP error loading entries: %s", exc)
            return []
        except RuntimeError as exc:
            # backend.read() raises RuntimeError for files >1 MB.
            logger.warning(
                "feedback store: cannot load entries — %s. "
                "Consider lowering AI_FEEDBACK_RETENTION_COUNT.",
                exc,
            )
            return []
        except Exception:
            logger.error("feedback store: unexpected error in load_recent", exc_info=True)
            return []

    def _append_once(self, entry: FeedbackEntry) -> None:
        """Read-modify-write the JSONL file.

        Wire format: the file is always stored **oldest-first** (one entry per
        line, chronological order).  In-memory ``apply_retention`` operates on
        newest-first lists, so we reverse on the write path.  Keeping the file
        order canonical lets multi-append runs round-trip without scrambling.
        """
        content, version = self.backend.read()
        existing = content if content is not None else ""

        # Parse existing (file is oldest-first; _parse_jsonl returns newest-first)
        existing_entries = self._parse_jsonl(existing)

        duplicate_of = _find_recent_duplicate(entry, existing_entries)
        if duplicate_of is not None:
            message = (
                f"feedback store: skipped a duplicate append for "
                f"command={entry.command!r} finding_id={entry.extras.get('finding_id')!r} "
                f"-- an entry at {duplicate_of.ts} already recorded this verdict; "
                f"new entry was at {entry.ts}."
            )
            # Emit a GitHub Actions ::warning:: annotation only when running
            # inside GitHub Actions to avoid polluting local/test output --
            # same gating convention as vcs/github.py's prior-reviews-fetch
            # warning. Must go to stderr, not stdout (the default `print`
            # target): cli.py's `dismiss`/`dismiss-inline` commands print
            # their actual PR reply text to stdout via `click.echo(...)`
            # *after* this append() call returns, and the calling workflow
            # step captures that stdout verbatim as the comment body to post.
            # A stray stdout write here landed ahead of the real reply and
            # was posted to the PR as a literal `::warning::...` line glued
            # onto the front of the reply -- confirmed live on PR #911,
            # comment https://github.com/tag1consulting/ai-pr-review/pull/911#issuecomment-5802793637.
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::warning::{message}", file=sys.stderr, flush=True)
            logger.warning(message)
            return

        # Prepend the new entry to keep newest-first semantics
        all_entries = [entry, *existing_entries]
        kept = apply_retention(
            all_entries,
            max_count=self.retention_count,
            max_age_days=self.retention_age_days,
        )
        # Write oldest-first to disk (reversed from in-memory newest-first)
        new_content = "\n".join(e.to_json() for e in reversed(kept)) + "\n"
        self.backend.write(new_content, version)

    @staticmethod
    def _parse_jsonl(content: str) -> list[FeedbackEntry]:
        """Parse JSONL into FeedbackEntry list (newest-first).

        The on-disk format is oldest-first; this method reverses to newest-first
        so callers get the standard ordering for retention and injection.
        Malformed lines are skipped with a WARNING log.
        """
        entries: list[FeedbackEntry] = []
        for lineno, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            e = FeedbackEntry.from_json(line)
            if e is None:
                logger.warning(
                    "feedback store: skipping malformed JSONL line %d: %r",
                    lineno, line[:80],
                )
                continue
            entries.append(e)
        entries.reverse()
        return entries


# ---------------------------------------------------------------------------
# _GitHubContentsBackend: GitHub Contents API backend
# ---------------------------------------------------------------------------

@dataclass
class _GitHubContentsBackend:
    """GitHub Contents API backend for `_StoreCore` (the HTTP mechanics
    `GitBranchStore` used to hold inline before this extraction."""

    repo: str
    branch: str
    token: str = field(repr=False)  # redacted from repr/log output
    client: httpx.Client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _file_url(self) -> str:
        return (
            f"https://api.github.com/repos/{self.repo}/contents/{_STORE_PATH}"
            f"?ref={self.branch}"
        )

    def _fetch_file_meta(self) -> tuple[str | None, str | None]:
        """Return (raw_content_b64, sha) or (None, None) if file not found.

        Raises ``RuntimeError`` when the GitHub Contents API omits ``content``
        for a file that exists (i.e. file > 1 MB): silently treating this as
        empty would overwrite the entire feedback history on the next append.
        """
        resp = self.client.get(self._file_url(), headers=self._headers())
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        data = resp.json()
        sha = data.get("sha")
        content = data.get("content")
        if sha is not None and content is None:
            raise RuntimeError(
                f"GitHub Contents API returned sha={sha!r} with no 'content' field "
                f"for {_STORE_PATH} (file may exceed 1 MB). "
                "Aborting to avoid destroying feedback history."
            )
        return content or "", sha

    def read(self) -> tuple[str | None, str | None]:
        b64, sha = self._fetch_file_meta()
        if b64 is None:
            return None, None
        raw = base64.b64decode(b64.replace("\n", ""))
        return raw.decode("utf-8", errors="replace"), sha

    def write(self, content: str, version: object | None) -> None:
        new_b64 = base64.b64encode(content.encode()).decode()

        payload: dict[str, object] = {
            "message": "chore: update AI review feedback store",
            "content": new_b64,
            "branch": self.branch,
        }
        if version:
            payload["sha"] = version

        url = f"https://api.github.com/repos/{self.repo}/contents/{_STORE_PATH}"
        resp = self.client.put(url, headers=self._headers(), json=payload)
        if resp.status_code == 409:
            raise _ConflictError(409)
        # GitHub returns 404 with "Branch ... not found" when the branch
        # doesn't exist on a PUT (observed behavior, May 2026).  Some older
        # docs say 422; handle both for forward compatibility.  We confirm
        # via branch_exists() so a 404 on a missing file (vs. missing
        # branch) isn't misinterpreted.
        if resp.status_code in (404, 422):
            # branch_exists is tri-state: only treat a definitive False as
            # "branch missing"; None (transient error) must surface as a
            # generic failure rather than triggering an unnecessary
            # branch-creation attempt.
            if self.branch_exists() is False:
                raise _MissingBranchError(self.branch)
            # If the branch DOES exist but we got 404/422, that's a genuine
            # validation error: surface it with the response body so the
            # operator can diagnose.
            raise RuntimeError(
                f"GitHub Contents API returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        resp.raise_for_status()

    def branch_exists(self) -> bool | None:
        """Tri-state branch existence check.

        Returns:
            True  — branch exists (HTTP 200)
            False — branch confirmed missing (HTTP 404)
            None  — unknown (transient transport error, auth failure, or
                    unexpected status); caller should NOT proceed with
                    bootstrap on None, to avoid creating a branch from a
                    misdiagnosed network blip.
        """
        url = f"https://api.github.com/repos/{self.repo}/branches/{self.branch}"
        try:
            resp = self.client.get(url, headers=self._headers())
        except httpx.TransportError as exc:
            logger.warning(
                "feedback store: branch existence check transport error: %s", exc,
            )
            return None
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        logger.warning(
            "feedback store: branch existence check returned unexpected status %d: %s",
            resp.status_code, resp.text[:200],
        )
        return None

    def bootstrap_branch(self) -> bool:
        """Create ``self.branch`` from the repo's default branch HEAD.

        Returns True on success, False on failure (logs a WARNING with the
        HTTP status code distinguishing 401/403 auth failures from 404 missing-
        repo from transient transport errors).  Called from `_StoreCore.append`
        when `_append_once` raises ``_MissingBranchError``.
        """
        try:
            repo_resp = self.client.get(
                f"https://api.github.com/repos/{self.repo}",
                headers=self._headers(),
            )
        except httpx.TransportError as exc:
            logger.warning(
                "feedback store: bootstrap aborted — transport error fetching repo: %s",
                exc,
            )
            return False
        if repo_resp.status_code != 200:
            logger.warning(
                "feedback store: bootstrap aborted — GET /repos/%s returned %d: %s",
                self.repo, repo_resp.status_code, repo_resp.text[:200],
            )
            return False
        try:
            default_branch = repo_resp.json().get("default_branch") or "main"
        except ValueError as exc:
            logger.warning(
                "feedback store: bootstrap aborted — bad JSON from /repos: %s", exc,
            )
            return False

        try:
            head_resp = self.client.get(
                f"https://api.github.com/repos/{self.repo}/git/ref/heads/{default_branch}",
                headers=self._headers(),
            )
        except httpx.TransportError as exc:
            logger.warning(
                "feedback store: bootstrap aborted — transport error fetching default HEAD: %s",
                exc,
            )
            return False
        if head_resp.status_code != 200:
            logger.warning(
                "feedback store: bootstrap aborted — GET refs/heads/%s returned %d: %s",
                default_branch, head_resp.status_code, head_resp.text[:200],
            )
            return False
        try:
            head_sha = head_resp.json().get("object", {}).get("sha")
        except ValueError as exc:
            logger.warning(
                "feedback store: bootstrap aborted — bad JSON from refs/heads: %s", exc,
            )
            return False
        if not head_sha:
            logger.warning(
                "feedback store: bootstrap aborted — could not resolve %r HEAD sha",
                default_branch,
            )
            return False

        try:
            create_resp = self.client.post(
                f"https://api.github.com/repos/{self.repo}/git/refs",
                headers=self._headers(),
                json={"ref": f"refs/heads/{self.branch}", "sha": head_sha},
            )
        except httpx.TransportError as exc:
            logger.warning(
                "feedback store: bootstrap aborted — transport error creating ref: %s",
                exc,
            )
            return False
        # 201 = created; 422 = already exists (race with concurrent run) — both OK
        if create_resp.status_code not in (201, 422):
            logger.warning(
                "feedback store: branch create returned %d: %s "
                "(401/403 = token lacks contents:write; 404 = repo missing)",
                create_resp.status_code, create_resp.text[:200],
            )
            return False
        logger.info(
            "feedback store: bootstrapped branch %r from %r@%s",
            self.branch, default_branch, head_sha[:7],
        )
        return True


# ---------------------------------------------------------------------------
# GitBranchStore: GitHub implementation (thin wrapper over _StoreCore)
# ---------------------------------------------------------------------------

@dataclass
class GitBranchStore:
    """Persist feedback entries in a JSONL file on a dedicated git branch.

    Parameters
    ----------
    repo:
        ``owner/repo`` slug (from ``GITHUB_REPOSITORY``).
    branch:
        Branch name (default ``ai-pr-review-bot``).
    token:
        GitHub token with ``contents:write`` on the target branch.
    retention_count:
        Maximum number of entries to keep (rolling window).
    retention_age_days:
        Drop entries older than this many days.
    client:
        Injected httpx.Client for testability.
    """

    repo: str
    branch: str
    token: str = field(repr=False)  # redacted from repr/log output
    retention_count: int = 500
    retention_age_days: int = 365
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=15))

    def __post_init__(self) -> None:
        self._backend = _GitHubContentsBackend(
            repo=self.repo, branch=self.branch, token=self.token, client=self.client,
        )
        self._core = _StoreCore(
            backend=self._backend,
            retention_count=self.retention_count,
            retention_age_days=self.retention_age_days,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, entry: FeedbackEntry) -> bool:
        return self._core.append(entry)

    def load_recent(self) -> list[FeedbackEntry]:
        return self._core.load_recent()

    # ------------------------------------------------------------------
    # Internal helpers kept as thin delegating wrappers -- exercised
    # directly by tests/python/test_feedback_store.py (issue #906 PR 2/3:
    # this refactor must leave that suite passing unchanged).
    # ------------------------------------------------------------------

    def _fetch_file_meta(self) -> tuple[str | None, str | None]:
        return self._backend._fetch_file_meta()

    def _branch_exists(self) -> bool | None:
        return self._backend.branch_exists()

    def _bootstrap_branch(self) -> bool:
        return self._backend.bootstrap_branch()

    def _append_once(self, entry: FeedbackEntry) -> None:
        self._core._append_once(entry)

    @staticmethod
    def _parse_jsonl(content: str) -> list[FeedbackEntry]:
        return _StoreCore._parse_jsonl(content)


class _ConflictError(Exception):
    """Internal: optimistic-lock conflict on PUT."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class _MissingBranchError(Exception):
    """Internal: PUT to a branch that does not exist yet.

    Distinct from ``_ConflictError`` so ``append()`` can take the bootstrap
    code path (create the branch, retry once) rather than burning retries.
    """

    def __init__(self, branch: str) -> None:
        self.branch = branch
        super().__init__(f"branch {branch!r} does not exist")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_store(config: object) -> FeedbackStore:
    """Build the appropriate store from *config*.

    Reads ``config.vcs_provider``, ``config.feedback_branch``,
    ``config.feedback_retention_count``, ``config.feedback_retention_age_days``.
    Falls back to ``UnsupportedVcsStore`` for non-GitHub VCS providers.
    """
    import os

    vcs = getattr(config, "vcs_provider", "").lower()
    if vcs != "github":
        return UnsupportedVcsStore()

    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        logger.warning("feedback store: no GH_TOKEN / GITHUB_TOKEN; store disabled")
        return UnsupportedVcsStore()

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        logger.warning("feedback store: no GITHUB_REPOSITORY; store disabled")
        return UnsupportedVcsStore()

    return GitBranchStore(
        repo=repo,
        branch=getattr(config, "feedback_branch", "ai-pr-review-bot"),
        token=token,
        retention_count=getattr(config, "feedback_retention_count", 500),
        retention_age_days=getattr(config, "feedback_retention_age_days", 365),
    )
