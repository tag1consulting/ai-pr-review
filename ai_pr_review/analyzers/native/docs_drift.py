"""Native Python implementation of the docs-drift analyzer.

Detects PR diffs that delete a file which is still referenced by
documentation elsewhere in the repo (a "stale reference" / documentation
drift). Unlike most native analyzers, this one does not filter by
``changed_files`` at all — it deliberately runs unconditionally so it can
catch an UNCHANGED doc that references a file this PR deletes.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 80
_SOURCE = "docs-drift-check"
_TIMEOUT_SECS = 30

_DIFF_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _parse_deleted_and_added_paths(diff_text: str) -> tuple[set[str], set[str]]:
    """Walk unified diff text and collect deleted and added file paths.

    Tracks ``diff --git a/(.+) b/(.+)`` headers, and for each file block
    records whether it saw ``+++ /dev/null`` (deleted) or ``--- /dev/null``
    (added). Paths are taken from the ``b/`` side of the header, since git
    always uses that as the canonical path even for deletions.
    """
    deleted_paths: set[str] = set()
    added_paths: set[str] = set()
    current_path: str | None = None

    for line in diff_text.splitlines():
        header_match = _DIFF_GIT_HEADER.match(line)
        if header_match:
            current_path = header_match.group(2)
            continue
        if current_path is None:
            continue
        if line.startswith("+++ /dev/null"):
            deleted_paths.add(current_path)
        elif line.startswith("--- /dev/null"):
            added_paths.add(current_path)

    return deleted_paths, added_paths


def _filter_renamed_paths(deleted_paths: set[str], added_paths: set[str]) -> set[str]:
    """Drop deleted paths whose basename also appears among added paths.

    This repo's diff computation does not request ``-M``/``--find-renames``,
    so a ``git mv`` shows as a plain delete-old + add-new pair rather than a
    paired rename entry. This is a deliberate basename-only heuristic scoped
    to file paths, not symbols or content similarity.
    """
    added_basenames = {Path(p).name for p in added_paths}
    return {p for p in deleted_paths if Path(p).name not in added_basenames}


def _run_docs_drift_check(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Find stale documentation references to files deleted in this diff."""
    try:
        diff_text = diff_file.read_text()
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: could not read diff file %s: %s", diff_file, exc)
        return []
    deleted_paths, added_paths = _parse_deleted_and_added_paths(diff_text)
    deleted_paths = _filter_renamed_paths(deleted_paths, added_paths)

    if not deleted_paths:
        return []

    if not shutil.which("rg"):
        logger.warning("[ai-pr-review] WARNING: rg not found; skipping.")
        return []

    findings: list[Finding] = []
    for deleted_path in sorted(deleted_paths):
        try:
            result = subprocess.run(
                [
                    "rg",
                    "--fixed-strings",
                    "--line-number",
                    "--with-filename",
                    "-g", "*.md",
                    "-g", "*.txt",
                    "-g", "*.rst",
                    "-g", "!vendor/*",
                    "-g", "!node_modules/*",
                    "--",
                    deleted_path,
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECS,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning("[ai-pr-review] WARNING: rg timed out after %ss; skipping.", exc.timeout)
            continue
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: rg failed to start: %s", exc)
            continue

        if result.returncode not in (0, 1):
            logger.warning(
                "[ai-pr-review] WARNING: rg exited %d; skipping. stderr: %s",
                result.returncode, result.stderr[:200],
            )
            continue

        if not result.stdout.strip():
            continue

        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            file_path, line_no_str, _matched_line = parts
            if file_path.startswith("./"):
                file_path = file_path[2:]

            # No self-reference noise from a file about to disappear
            # referencing itself in its own deletion diff.
            if file_path in deleted_paths:
                continue

            try:
                line_no = int(line_no_str)
            except ValueError:
                logger.warning(
                    "[ai-pr-review] WARNING: rg produced non-numeric line number: %r; skipping match.",
                    line_no_str,
                )
                continue

            try:
                findings.append(
                    Finding(
                        severity="Low",
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=file_path,
                        line=line_no,
                        finding=(
                            f"References deleted file '{deleted_path}', "
                            "which no longer exists in this PR."
                        ),
                        remediation=f"Update or remove the reference to '{deleted_path}'.",
                        category="docs",
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: docs-drift-check dropped malformed finding: %s; "
                    "file=%r line=%r", exc, file_path, line_no_str,
                )

    return findings
