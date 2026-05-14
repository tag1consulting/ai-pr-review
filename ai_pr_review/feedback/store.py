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
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.feedback.retention import apply_retention

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_S = 0.5
_STORE_PATH = ".ai-pr-review/learnings.jsonl"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class FeedbackStore(Protocol):
    """Read/write interface for the feedback store."""

    def append(self, entry: FeedbackEntry) -> None:
        """Append one entry.  Fail-soft: log WARNING on error."""
        ...

    def load_recent(self) -> list[FeedbackEntry]:
        """Return all stored entries (newest first).  Return [] on error."""
        ...


# ---------------------------------------------------------------------------
# UnsupportedVcsStore — stub for GitLab / Bitbucket
# ---------------------------------------------------------------------------

class UnsupportedVcsStore:
    """No-op store for VCS providers where feedback loop is not yet implemented."""

    def append(self, entry: FeedbackEntry) -> None:
        logger.info("feedback store: VCS provider not supported; entry dropped")

    def load_recent(self) -> list[FeedbackEntry]:
        return []


# ---------------------------------------------------------------------------
# GitBranchStore — GitHub implementation
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
    token: str
    retention_count: int = 500
    retention_age_days: int = 365
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=15))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, entry: FeedbackEntry) -> None:
        """Append *entry* with optimistic-lock retry."""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._append_once(entry)
                return
            except _ConflictError:
                if attempt == _MAX_RETRIES:
                    logger.warning(
                        "feedback store: SHA conflict after %d attempts; entry dropped",
                        _MAX_RETRIES,
                    )
                    return
                jitter = random.uniform(0, _RETRY_BASE_S * attempt)
                time.sleep(_RETRY_BASE_S * attempt + jitter)
            except Exception as exc:
                logger.warning("feedback store: append failed: %s", exc)
                return

    def load_recent(self) -> list[FeedbackEntry]:
        """Return all entries (newest-first).  Return [] on any error."""
        try:
            content = self._fetch_file_content()
            if content is None:
                return []
            return self._parse_jsonl(content)
        except Exception as exc:
            logger.warning("feedback store: load failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        """Return (raw_content_b64, sha) or (None, None) if file not found."""
        resp = self.client.get(self._file_url(), headers=self._headers())
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        data = resp.json()
        return data.get("content", ""), data.get("sha")

    def _fetch_file_content(self) -> str | None:
        """Return decoded file content, or None if file doesn't exist."""
        b64, _sha = self._fetch_file_meta()
        if b64 is None:
            return None
        raw = base64.b64decode(b64.replace("\n", ""))
        return raw.decode("utf-8", errors="replace")

    def _append_once(self, entry: FeedbackEntry) -> None:
        b64_old, sha = self._fetch_file_meta()

        if b64_old is not None:
            existing = base64.b64decode(b64_old.replace("\n", "")).decode(
                "utf-8", errors="replace"
            )
        else:
            existing = ""

        # Append new line then apply retention
        lines = existing.splitlines()
        lines.append(entry.to_json())
        kept = apply_retention(
            self._parse_jsonl("\n".join(lines)),
            max_count=self.retention_count,
            max_age_days=self.retention_age_days,
        )
        new_content = "\n".join(e.to_json() for e in kept) + "\n"
        new_b64 = base64.b64encode(new_content.encode()).decode()

        payload: dict[str, object] = {
            "message": "chore: update AI review feedback store",
            "content": new_b64,
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha

        url = (
            f"https://api.github.com/repos/{self.repo}/contents/{_STORE_PATH}"
        )
        resp = self.client.put(url, headers=self._headers(), json=payload)
        if resp.status_code in (409, 422):
            raise _ConflictError(resp.status_code)
        resp.raise_for_status()

    @staticmethod
    def _parse_jsonl(content: str) -> list[FeedbackEntry]:
        """Parse JSONL into FeedbackEntry list (newest-first, skipping bad lines)."""
        entries = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            e = FeedbackEntry.from_json(line)
            if e is not None:
                entries.append(e)
        entries.reverse()
        return entries


class _ConflictError(Exception):
    """Internal: optimistic-lock conflict on PUT."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_store(config: object) -> FeedbackStore:
    """Build the appropriate store from *config*.

    Reads ``config.provider``, ``config.feedback_branch``,
    ``config.feedback_retention_count``, ``config.feedback_retention_age_days``.
    Falls back to ``UnsupportedVcsStore`` for non-GitHub providers.
    """
    import os

    provider = getattr(config, "provider", "").lower()
    if provider not in ("github", "bedrock-proxy"):
        return UnsupportedVcsStore()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        logger.warning("feedback store: no GH_TOKEN / GITHUB_TOKEN; store disabled")
        return UnsupportedVcsStore()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
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
