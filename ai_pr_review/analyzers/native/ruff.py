"""Native Python implementation of the ruff analyzer.

Replaces analyzers/run-ruff.sh. Invokes ruff directly via subprocess and
converts its JSON output to Finding instances.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "ruff"
_TIMEOUT_SECS = 120


def _run_ruff(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run ruff on changed Python files and return Finding instances."""
    py_files = [f for f in changed_files.python if Path(f).is_file()]
    if not py_files:
        return []

    if not shutil.which("ruff"):
        logger.warning("[ai-pr-review] WARNING: ruff not found; skipping.")
        return []

    try:
        result = subprocess.run(
            ["ruff", "check", "--output-format=json", "--no-cache", "--exit-zero", "--", *py_files],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: ruff timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: ruff failed to start: %s", exc)
        return []

    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: ruff exited %d; skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: ruff produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, list):
        logger.warning("[ai-pr-review] WARNING: ruff produced unexpected output structure (not a list); skipping.")
        return []

    workspace_prefix = (os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).rstrip("/") + "/"

    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = item.get("code") or ""
        prefix = code[:1]
        if prefix in ("F", "E"):
            severity = "High"
        elif prefix in ("W", "C"):
            severity = "Medium"
        else:
            severity = "Low"

        filename = item.get("filename") or ""
        if filename.startswith(workspace_prefix):
            filename = filename[len(workspace_prefix):]

        url = item.get("url")
        remediation = f"See {url}" if url else f"See https://docs.astral.sh/ruff/rules/{code}"

        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=filename,
                    line=item.get("location", {}).get("row") or None,
                    finding=f"{code}: {item.get('message', '')}",
                    remediation=remediation,
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: ruff dropped malformed finding: %s; item=%r",
                exc, repr(item)[:200],
            )

    return findings
