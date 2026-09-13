"""Native Python implementation of the shellcheck analyzer.

Replaces analyzers/run-shellcheck.sh. Invokes shellcheck directly via
subprocess and converts its json1 output to Finding instances.
"""

from __future__ import annotations

import logging
import shutil

# subprocess is never called directly in this module (run_cli_json_analyzer
# owns the actual subprocess.run call) but stays imported: existing tests
# patch it as "ai_pr_review.analyzers.native.shellcheck.subprocess.run",
# which resolves the attribute on *this* module first. Since `subprocess` is
# a singleton module object, the patch still lands on the real subprocess.run
# that _cli_runner.py calls -- removing the import would only break the
# test's attribute lookup, not the patch's effect.
import subprocess  # noqa: F401
from pathlib import Path
from typing import Any

from ai_pr_review.analyzers.native._cli_runner import run_cli_json_analyzer
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 95
_SOURCE = "shellcheck"
_TIMEOUT_SECS = 120


def _run_shellcheck(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run shellcheck on changed shell files and return Finding instances."""
    shell_files = [f for f in changed_files.shell if Path(f).is_file()]
    if not shell_files:
        return []

    if not shutil.which("shellcheck"):
        logger.warning("[ai-pr-review] WARNING: shellcheck not found; skipping.")
        return []

    findings: list[Finding] = []
    for file_path in shell_files:
        findings.extend(_scan_file(file_path))
    return findings


def _scan_file(file_path: str) -> list[Finding]:
    return run_cli_json_analyzer(
        tool="shellcheck",
        command=["shellcheck", "-f", "json1", "-S", "warning", "--", file_path],
        timeout_secs=_TIMEOUT_SECS,
        extract_items=lambda data: _shellcheck_items(data, file_path),
        build_finding=lambda item: _shellcheck_finding(item, file_path),
    )


def _shellcheck_items(data: Any, file_path: str) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict):
        logger.warning(
            "[ai-pr-review] WARNING: shellcheck produced unexpected output structure for %r; skipping.",
            file_path,
        )
        return None
    comments = data.get("comments") or []
    return [c for c in comments if isinstance(c, dict)]


def _shellcheck_finding(item: dict[str, Any], file_path: str) -> Finding:
    level = item.get("level", "")
    if level == "error":
        severity = "High"
    elif level == "warning":
        severity = "Medium"
    else:
        severity = "Low"
    code = item.get("code", 0)
    message = item.get("message", "")
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        confidence=_CONFIDENCE,
        source=_SOURCE,
        file=file_path,
        line=item.get("line") or None,
        finding=f"SC{code}: {message}",
        remediation=f"See https://www.shellcheck.net/wiki/SC{code}",
        category="lint",
    )
