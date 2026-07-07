"""Native Python implementation of the phpcs analyzer.

Replaces analyzers/run-phpcs.sh. Invokes phpcs directly via subprocess and
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
_SOURCE = "phpcs"
_TIMEOUT_SECS = 120
_PHP_EXTENSIONS = {".php", ".module", ".inc", ".theme", ".install", ".profile"}


def _detect_standard() -> str:
    """Return the phpcs coding standard to use: Drupal,DrupalPractice if available, else PSR12."""
    try:
        result = subprocess.run(
            ["phpcs", "-i"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "Drupal" in result.stdout:
            return "Drupal,DrupalPractice"
    except subprocess.TimeoutExpired:
        logger.warning("[ai-pr-review] WARNING: phpcs -i timed out detecting standard; falling back to PSR12.")
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs -i failed: %s; falling back to PSR12.", exc)
    return "PSR12"


def _run_phpcs(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run phpcs on changed PHP files and return Finding instances."""
    php_files = [f for f in changed_files.php if Path(f).is_file() and Path(f).suffix in _PHP_EXTENSIONS]
    if not php_files:
        return []

    if not shutil.which("phpcs"):
        logger.warning("[ai-pr-review] WARNING: phpcs not found; skipping.")
        return []

    standard = _detect_standard()

    try:
        result = subprocess.run(
            [
                "phpcs",
                "--report=json",
                f"--standard={standard}",
                "--extensions=php,module,inc,theme,install,profile",
                "-q",
                "--",
                *php_files,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs failed to start: %s", exc)
        return []

    # Exit codes: 0 = clean, 1 = violations found, 2 = config/fatal error.
    if result.returncode == 2:
        logger.warning(
            "[ai-pr-review] WARNING: phpcs exited 2 (config/fatal error); standard=%r may not be installed. stderr: %s",
            standard, result.stderr[:200],
        )
        return []
    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: phpcs exited %d; skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: phpcs produced unexpected output structure; skipping.")
        return []

    pwd_prefix = os.getcwd().rstrip("/") + "/"
    findings: list[Finding] = []

    for file_path, file_data in (data.get("files") or {}).items():
        if not isinstance(file_data, dict):
            continue
        normalized_path = file_path
        if normalized_path.startswith(pwd_prefix):
            normalized_path = normalized_path[len(pwd_prefix):]

        for msg in file_data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type", "")
            severity = "High" if msg_type == "ERROR" else "Medium"
            source_rule = msg.get("source") or ""
            try:
                findings.append(
                    Finding(
                        severity=severity,  # type: ignore[arg-type]
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=normalized_path,
                        line=msg.get("line") or None,
                        finding=f"{source_rule}: {msg.get('message', '')}",
                        remediation=f"See https://www.drupal.org/docs/develop/standards or fix with: phpcs --standard={standard} {normalized_path}",
                        category="lint",
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: phpcs dropped malformed finding: %s; msg=%r",
                    exc, repr(msg)[:200],
                )

    return findings
