"""Data shapes shared between platforms.py (does real HTTP I/O) and verify.py
(pure functions, no I/O). Kept in their own module with no I/O imports of its
own, so verify.py's "pure, no I/O" claim doesn't rest on merely not calling
anything in platforms.py -- it can't, because it never imports the module
that does the I/O in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AdapterError(RuntimeError):
    """Raised by an adapter on an unrecoverable API failure. Always carries
    a mask()-ed message -- construct via `AdapterError(mask(str(exc)))`."""


@dataclass(frozen=True)
class RunCommit:
    platform: str
    run_id: str
    branch: str
    commit_sha: str


@dataclass(frozen=True)
class PullRequest:
    platform: str
    number: int
    url: str
    branch: str
    run_commit: RunCommit


@dataclass
class RawEvidence:
    """Everything fetched back from a platform about one run's PR/MR."""

    summary_body: str = ""
    summary_hidden_markers: list[str] = field(default_factory=list)
    inline_comments: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)  # Bitbucket only
    truncated: bool = False
