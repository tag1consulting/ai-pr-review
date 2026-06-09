"""Native Python implementation of the shellcheck analyzer.

Replaces analyzers/run-shellcheck.sh. Invokes shellcheck directly via
subprocess and converts its json1 output to Finding instances.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

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
    try:
        result = subprocess.run(
            ["shellcheck", "-f", "json1", "-S", "warning", "--", file_path],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("[ai-pr-review] WARNING: shellcheck failed for %r: %s", file_path, exc)
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: shellcheck produced non-JSON output for %r: %s", file_path, exc)
        return []

    findings: list[Finding] = []
    for item in data.get("comments") or []:
        if not isinstance(item, dict):
            continue
        level = item.get("level", "")
        if level == "error":
            severity = "High"
        elif level == "warning":
            severity = "Medium"
        else:
            severity = "Low"
        code = item.get("code", 0)
        message = item.get("message", "")
        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=file_path,
                    line=item.get("line"),
                    finding=f"SC{code}: {message}",
                    remediation=f"See https://www.shellcheck.net/wiki/SC{code}",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning("[ai-pr-review] WARNING: shellcheck dropped malformed finding: %s", exc)

    return findings
