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
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "phpcs"
_TIMEOUT_SECS = 120
_PHP_EXTENSIONS = {".php", ".module", ".inc", ".theme", ".install", ".profile"}

# phpcs's --standard flag only accepts full ruleset names (or a path to a
# ruleset.xml); it cannot mix a named standard with a bare sniff code
# (`--standard=PSR12,Squiz.Commenting.FunctionComment` errors: "the
# Squiz.Commenting.FunctionComment coding standard is not installed").
# Verified empirically against phpcs 4.0.4 this session. A small ruleset.xml
# combining PSR12 with the one Squiz docblock sniff is the documented way to
# add a single sniff on top of a named standard.
_PSR12_PLUS_DOCBLOCK_RULESET = """<?xml version="1.0"?>
<ruleset name="PSR12PlusDocblock">
    <description>PSR12 with Squiz function-comment sniffs added.</description>
    <rule ref="PSR12"/>
    <rule ref="Squiz.Commenting.FunctionComment"/>
</ruleset>
"""

# phpcs 4.x exit codes are a bitmask (PHPCSStandards/PHP_CodeSniffer#184,
# src/Util/ExitCode.php): 0=clean, 1=fixable violations, 2=non-fixable
# violations, 3=both. All four mean "phpcs completed a normal run; the JSON
# output is trustworthy." Fatal/config errors use 16 (PROCESS_ERROR) or 64
# (REQUIREMENTS_NOT_MET) instead, which never overlap this range. This
# differs from phpcs 3.x, where 2 meant "some fixable issue exists" and a
# genuine config error was not representable via this simple 0/1/2 scheme.
_SUCCESS_RETURNCODES = frozenset({0, 1, 2, 3})


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


@contextmanager
def _standard_arg(standard: str) -> Iterator[str]:
    """Return the value to pass as --standard for *standard*.

    Named standards pass through unchanged. The PSR12 fallback is upgraded to
    a temp-file ruleset combining PSR12 with the Squiz docblock sniff, which
    extends the doc-comment/signature mismatch coverage Drupal repos already
    get (via Drupal.Commenting.FunctionComment) to non-Drupal PHP repos.
    """
    if standard != "PSR12":
        yield standard
        return
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", prefix="phpcs-ruleset-", delete=True
    ) as f:
        f.write(_PSR12_PLUS_DOCBLOCK_RULESET)
        f.flush()
        yield f.name


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
        with _standard_arg(standard) as standard_arg:
            result = subprocess.run(
                [
                    "phpcs",
                    "--report=json",
                    f"--standard={standard_arg}",
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

    if result.returncode not in _SUCCESS_RETURNCODES:
        logger.warning(
            "[ai-pr-review] WARNING: phpcs exited %d (process/config error); standard=%r may not be installed. "
            "stderr: %s",
            result.returncode, standard, result.stderr[:200],
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
