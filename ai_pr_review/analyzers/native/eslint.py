"""Native Python implementation of the eslint analyzer.

Replaces analyzers/run-eslint.sh. Resolves the eslint binary (local
node_modules or npx), checks for an eslint config file, and converts
ESLint's JSON output to Finding instances.
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
_SOURCE = "eslint"
_TIMEOUT_SECS = 120

_JS_TS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})

_ESLINT_CONFIG_NAMES = (
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
    ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.yaml", ".eslintrc.yml",
    ".eslintrc.json", ".eslintrc",
)


def _find_eslint_bin() -> list[str] | None:
    """Return the eslint command as a list, or None if not found."""
    local_bin = Path("./node_modules/.bin/eslint")
    if local_bin.is_file() and os.access(str(local_bin), os.X_OK):
        return [str(local_bin)]
    if shutil.which("npx"):
        try:
            result = subprocess.run(
                ["npx", "--no", "eslint", "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return ["npx", "eslint"]
        except (subprocess.TimeoutExpired, OSError):
            pass
    return None


def _has_eslint_config() -> bool:
    """Return True if an eslint config file is present in CWD or GITHUB_WORKSPACE."""
    search_dirs: list[Path] = [Path(".")]
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    if workspace:
        search_dirs.append(Path(workspace))
    for cfg in _ESLINT_CONFIG_NAMES:
        for d in search_dirs:
            if (d / cfg).is_file():
                return True
    return False


def _supports_no_warn_ignored(eslint_cmd: list[str]) -> bool:
    """Return True if the eslint binary accepts --no-warn-ignored."""
    try:
        result = subprocess.run(
            [*eslint_cmd, "--no-warn-ignored", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _run_eslint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run eslint on changed JS/TS files and return Finding instances."""
    target_files = [
        f for f in changed_files.js_ts
        if Path(f).suffix in _JS_TS_EXTENSIONS and Path(f).is_file()
    ]
    if not target_files:
        return []

    eslint_cmd = _find_eslint_bin()
    if eslint_cmd is None:
        logger.warning(
            "[ai-pr-review] WARNING: eslint not found "
            "(tried node_modules/.bin/eslint and npx); skipping."
        )
        return []

    if not _has_eslint_config():
        return []

    extra_flags: list[str] = []
    if _supports_no_warn_ignored(eslint_cmd):
        extra_flags.append("--no-warn-ignored")

    try:
        result = subprocess.run(
            [*eslint_cmd, "--format", "json", *extra_flags, *target_files],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: eslint timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: eslint failed to start: %s", exc)
        return []

    if result.returncode == 2:
        logger.warning(
            "[ai-pr-review] WARNING: eslint exited with fatal error (exit 2); "
            "broken config or missing plugin. stderr: %s",
            result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: eslint produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, list):
        logger.warning("[ai-pr-review] WARNING: eslint produced unexpected output structure; skipping.")
        return []

    cwd_prefix = str(Path.cwd()) + "/"
    findings: list[Finding] = []

    for file_entry in data:
        if not isinstance(file_entry, dict):
            continue
        file_path = file_entry.get("filePath") or ""
        rel_path = file_path[len(cwd_prefix):] if file_path.startswith(cwd_prefix) else file_path
        messages = file_entry.get("messages") or []
        if not isinstance(messages, list):
            continue

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            rule_id = msg.get("ruleId")
            if not rule_id:
                continue
            message = msg.get("message") or ""
            severity_code = msg.get("severity") or 1
            line = msg.get("line") or 1
            severity = "High" if severity_code == 2 else "Medium"
            remediation = f"See https://eslint.org/docs/rules/{rule_id}"

            try:
                findings.append(
                    Finding(
                        severity=severity,  # type: ignore[arg-type]
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=rel_path,
                        line=line,
                        finding=f"{rule_id}: {message}",
                        remediation=remediation,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: eslint dropped malformed finding: %s; msg=%r",
                    exc, repr(msg)[:200],
                )

    return findings
