"""Native Python implementation of the phpstan analyzer.

Replaces analyzers/run-phpstan.sh. Discovers config files and phpstan-drupal,
invokes phpstan, and converts its JSON output to Finding instances.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 85
_SOURCE = "phpstan"
_TIMEOUT_SECS = 120
_PHP_EXTENSIONS = frozenset({".php", ".module", ".inc", ".theme", ".install", ".profile"})
_REMEDIATION = "Fix the type error reported by PHPStan. See https://phpstan.org/user-guide/getting-started"
_VALID_LEVEL_RE = re.compile(r"^[0-9]$")
_DEFAULT_LEVEL = "3"


def _run_phpstan(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run phpstan on changed PHP files and return Finding instances."""
    target_files = [
        f for f in changed_files.php
        if Path(f).suffix in _PHP_EXTENSIONS and Path(f).is_file()
    ]
    if not target_files:
        return []

    if not shutil.which("phpstan"):
        logger.warning("[ai-pr-review] WARNING: phpstan not found; skipping.")
        return []

    level = os.environ.get("PHPSTAN_LEVEL", _DEFAULT_LEVEL).strip()
    if not _VALID_LEVEL_RE.match(level):
        logger.warning(
            "[ai-pr-review] WARNING: PHPSTAN_LEVEL='%s' is not a valid level (0-9); using 3.", level
        )
        level = _DEFAULT_LEVEL

    args = ["phpstan", "analyse", "--error-format=json", "--no-progress", "--memory-limit=512M"]

    has_config = Path("phpstan.neon").is_file() or Path("phpstan.neon.dist").is_file()
    if has_config:
        # Consumer config drives everything
        pass
    else:
        args.append(f"--level={level}")
        if Path("vendor/mglaman/phpstan-drupal").is_dir() and Path("vendor/autoload.php").is_file():
            args.append("--autoload-file=vendor/autoload.php")

    # `--` stops option parsing so an attacker-controlled changed-file path
    # beginning with a dash (e.g. `--autoload-file=evil.php`) is treated as a
    # path, not a flag. Matches the sibling analyzers (shellcheck, ruff, etc.).
    args.append("--")
    args += target_files

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: phpstan timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpstan failed to start: %s", exc)
        return []

    if result.returncode >= 2:
        logger.warning(
            "[ai-pr-review] WARNING: phpstan exited %d; possible configuration error. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: phpstan produced no output. stderr: %s",
            result.stderr[:200],
        )
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: phpstan produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: phpstan produced unexpected output structure; skipping.")
        return []

    files_block = data.get("files") or {}
    if not isinstance(files_block, dict):
        logger.warning("[ai-pr-review] WARNING: phpstan 'files' is not a dict; skipping.")
        return []

    cwd_prefix = str(Path.cwd()) + "/"
    findings: list[Finding] = []

    for file_path, file_data in files_block.items():
        if not isinstance(file_data, dict):
            continue
        messages = file_data.get("messages") or []
        if not isinstance(messages, list):
            continue
        # Strip CWD prefix so paths are repo-relative
        rel_path = file_path[len(cwd_prefix):] if file_path.startswith(cwd_prefix) else file_path

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            message = msg.get("message")
            if not message:
                continue
            line = msg.get("line") or 1

            try:
                findings.append(
                    Finding(
                        severity="High",
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=rel_path,
                        line=line,
                        finding=message,
                        remediation=_REMEDIATION,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: phpstan dropped malformed finding: %s; msg=%r",
                    exc, repr(msg)[:200],
                )

    return findings
