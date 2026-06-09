"""Native Python implementation of the semgrep analyzer.

Replaces analyzers/run-semgrep.sh. Invokes semgrep directly via subprocess
and converts its JSON output to Finding instances.
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
_SOURCE = "semgrep"
_TIMEOUT_SECS = 120


def _resolve_config() -> list[str]:
    """Return semgrep --config arguments in priority order.

    Priority:
    1. SEMGREP_RULES env var (explicit config string, e.g. "p/ci")
    2. .semgrep/ directory containing *.yml files
    3. semgrep.yml in the current directory
    4. --config=auto (network fetch fallback)
    """
    rules_env = os.environ.get("SEMGREP_RULES", "").strip()
    if rules_env:
        return ["--config", rules_env]

    semgrep_dir = Path(".semgrep")
    if semgrep_dir.is_dir():
        yml_files = sorted(semgrep_dir.glob("*.yml"))
        if yml_files:
            args: list[str] = []
            for yf in yml_files:
                # Use relative path (relative to CWD) consistent with bash wrapper.
                try:
                    rel = yf.relative_to(Path.cwd())
                    args += ["--config", str(rel)]
                except ValueError:
                    args += ["--config", str(yf)]
            return args

    if Path("semgrep.yml").is_file():
        return ["--config", "semgrep.yml"]

    return ["--config=auto"]


def _run_semgrep(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run semgrep on changed files and return Finding instances."""
    target_files = [f for f in changed_files.all_files if Path(f).is_file()]
    if not target_files:
        return []

    if not shutil.which("semgrep"):
        logger.warning("[ai-pr-review] WARNING: semgrep not found; skipping.")
        return []

    config_args = _resolve_config()

    try:
        result = subprocess.run(
            ["semgrep", "--json", "--quiet", *config_args, "--", *target_files],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: semgrep timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: semgrep failed to start: %s", exc)
        return []

    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: semgrep exited %d (possible network/config error); skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: semgrep produced no output — possible network failure or config error. stderr: %s",
            result.stderr[:200],
        )
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: semgrep produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: semgrep produced unexpected output structure; skipping.")
        return []

    results = data.get("results") or []
    if not isinstance(results, list):
        logger.warning("[ai-pr-review] WARNING: semgrep 'results' is not a list; skipping.")
        return []

    findings: list[Finding] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") or {}
        severity_raw = extra.get("severity", "")
        if severity_raw == "ERROR":
            severity = "High"
        elif severity_raw == "WARNING":
            severity = "Medium"
        else:
            severity = "Low"

        check_id = item.get("check_id") or ""
        message = extra.get("message") or ""
        metadata = extra.get("metadata") or {}
        references = metadata.get("references") or []
        ref = references[0] if references else None
        remediation = f"See {ref}" if ref else (f"Review the semgrep rule: {check_id}" if check_id else "No remediation available")

        start = item.get("start") or {}

        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=item.get("path") or "",
                    line=start.get("line") or None,
                    finding=f"{check_id}: {message}",
                    remediation=remediation,
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: semgrep dropped malformed finding: %s; item=%r",
                exc, repr(item)[:200],
            )

    return findings
