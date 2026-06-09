"""Native Python implementation of the hadolint analyzer.

Replaces analyzers/run-hadolint.sh. Invokes hadolint directly via subprocess
and converts its JSON output to Finding instances.
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

    try:
        result = subprocess.run(
            ["hadolint", "--format", "json", "--no-fail", "--", *dockerfile_files],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: hadolint timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: hadolint failed to start: %s", exc)
        return []

    if result.returncode != 0:
        logger.warning(
            "[ai-pr-review] WARNING: hadolint exited %d; skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: hadolint produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, list):
        logger.warning("[ai-pr-review] WARNING: hadolint produced unexpected output structure (not a list); skipping.")
        return []

    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        level = item.get("level", "")
        if level == "error":
            severity = "High"
        elif level == "warning":
            severity = "Medium"
        else:
            severity = "Low"

        code = item.get("code") or ""
        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=item.get("file") or "",
                    line=item.get("line") or None,
                    finding=f"{code}: {item.get('message', '')}",
                    remediation=f"See https://github.com/hadolint/hadolint/wiki/{code}",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: hadolint dropped malformed finding: %s; item=%r",
                exc, repr(item)[:200],
            )

    return findings
