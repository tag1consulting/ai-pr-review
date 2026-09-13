"""Native Python implementation of the hadolint analyzer.

Replaces analyzers/run-hadolint.sh. Invokes hadolint directly via subprocess
and converts its JSON output to Finding instances.
"""

from __future__ import annotations

import logging
import shutil

# subprocess is never called directly in this module (run_cli_json_analyzer
# owns the actual subprocess.run call) but stays imported: existing tests
# patch it as "ai_pr_review.analyzers.native.hadolint.subprocess.run", which
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
_SOURCE = "hadolint"
_TIMEOUT_SECS = 120

def _is_dockerfile(path: str) -> bool:
    p = Path(path)
    return p.name == "Dockerfile" or p.name.startswith("Dockerfile.") or p.suffix == ".dockerfile"


def _run_hadolint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run hadolint on changed Dockerfiles and return Finding instances."""
    dockerfile_files = [f for f in changed_files.dockerfile if Path(f).is_file() and _is_dockerfile(f)]
    if not dockerfile_files:
        return []

    if not shutil.which("hadolint"):
        logger.warning("[ai-pr-review] WARNING: hadolint not found; skipping.")
        return []

    return run_cli_json_analyzer(
        tool="hadolint",
        command=["hadolint", "--format", "json", "--no-fail", "--", *dockerfile_files],
        timeout_secs=_TIMEOUT_SECS,
        extract_items=_hadolint_items,
        build_finding=_hadolint_finding,
    )


def _hadolint_items(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, list):
        logger.warning(
            "[ai-pr-review] WARNING: hadolint produced unexpected output structure (not a list); skipping."
        )
        return None
    return [item for item in data if isinstance(item, dict)]


def _hadolint_finding(item: dict[str, Any]) -> Finding:
    level = item.get("level", "")
    if level == "error":
        severity = "High"
    elif level == "warning":
        severity = "Medium"
    else:
        severity = "Low"

    code = item.get("code") or ""
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        confidence=_CONFIDENCE,
        source=_SOURCE,
        file=item.get("file") or "",
        line=item.get("line") or None,
        finding=f"{code}: {item.get('message', '')}",
        remediation=f"See https://github.com/hadolint/hadolint/wiki/{code}",
        category="lint",
    )
