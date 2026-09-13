"""Native Python implementation of the ruff analyzer.

Replaces analyzers/run-ruff.sh. Invokes ruff directly via subprocess and
converts its JSON output to Finding instances.
"""

from __future__ import annotations

import logging
import os
import shutil

# subprocess is never called directly in this module (run_cli_json_analyzer
# owns the actual subprocess.run call) but stays imported: existing tests
# patch it as "ai_pr_review.analyzers.native.ruff.subprocess.run", which
# resolves the attribute on *this* module first. Since `subprocess` is a
# singleton module object, the patch still lands on the real subprocess.run
# that _cli_runner.py calls -- removing the import would only break the
# test's attribute lookup, not the patch's effect.
import subprocess  # noqa: F401
from pathlib import Path
from typing import Any

from ai_pr_review.analyzers.native._cli_runner import run_cli_json_analyzer
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

    return run_cli_json_analyzer(
        tool="ruff",
        command=["ruff", "check", "--output-format=json", "--no-cache", "--exit-zero", "--", *py_files],
        timeout_secs=_TIMEOUT_SECS,
        extract_items=_ruff_items,
        build_finding=_ruff_finding,
    )


def _ruff_items(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, list):
        logger.warning("[ai-pr-review] WARNING: ruff produced unexpected output structure (not a list); skipping.")
        return None
    return [item for item in data if isinstance(item, dict)]


def _ruff_finding(item: dict[str, Any]) -> Finding:
    workspace_prefix = (os.environ.get("GITHUB_WORKSPACE") or os.getcwd()).rstrip("/") + "/"

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

    return Finding(
        severity=severity,  # type: ignore[arg-type]
        confidence=_CONFIDENCE,
        source=_SOURCE,
        file=filename,
        line=item.get("location", {}).get("row") or None,
        finding=f"{code}: {item.get('message', '')}",
        remediation=remediation,
        category="lint",
    )
