"""Native Python implementation of the eslint analyzer.

Replaces analyzers/run-eslint.sh. Resolves the eslint binary from $PATH
only, checks for an eslint config file, and converts ESLint's JSON output
to Finding instances.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ai_pr_review.analyzers.native._cli_runner import run_cli_json_analyzer
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
    """Return the eslint command as a list, or None if not found.

    Deliberately resolves only via $PATH (shutil.which). The workspace is
    untrusted PR content in the container-action path, so this must never
    resolve a path relative to the working directory — not directly (e.g.
    ./node_modules/.bin/eslint) and not via `npx eslint`, which itself
    checks node_modules/.bin/eslint before consulting the registry and so
    is equally exploitable unless every invocation pins an exact version
    (which would add a network dependency this analyzer doesn't otherwise
    have). A checked-in executable in the analyzed tree would otherwise run
    with the review container's secrets (#738).
    """
    eslint_path = shutil.which("eslint")
    if eslint_path:
        return [eslint_path]
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
        logger.warning("[ai-pr-review] WARNING: no eslint config found; skipping.")
        return []

    extra_flags: list[str] = []
    if _supports_no_warn_ignored(eslint_cmd):
        extra_flags.append("--no-warn-ignored")

    return run_cli_json_analyzer(
        tool="eslint",
        command=[*eslint_cmd, "--format", "json", *extra_flags, "--", *target_files],
        timeout_secs=_TIMEOUT_SECS,
        # eslint's own exit codes: 0 = clean, 1 = lint issues found (not
        # fatal), 2 = fatal error (broken config or missing plugin). The
        # runner's default success set ({0, 1}) already treats 2 as failure;
        # this hook only swaps in eslint's more specific message for it.
        on_bad_returncode=_eslint_bad_returncode,
        extract_items=_eslint_items,
        build_finding=_eslint_finding,
    )


def _eslint_bad_returncode(result: subprocess.CompletedProcess[str]) -> None:
    logger.warning(
        "[ai-pr-review] WARNING: eslint exited with fatal error (exit 2); "
        "broken config or missing plugin. stderr: %s",
        result.stderr[:200],
    )


def _eslint_items(data: Any) -> list[tuple[str, dict[str, Any]]] | None:
    if not isinstance(data, list):
        logger.warning("[ai-pr-review] WARNING: eslint produced unexpected output structure; skipping.")
        return None

    cwd_prefix = str(Path.cwd()) + "/"
    items: list[tuple[str, dict[str, Any]]] = []
    for file_entry in data:
        if not isinstance(file_entry, dict):
            continue
        file_path = file_entry.get("filePath") or ""
        rel_path = file_path[len(cwd_prefix):] if file_path.startswith(cwd_prefix) else file_path
        messages = file_entry.get("messages") or []
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if isinstance(msg, dict):
                items.append((rel_path, msg))
    return items


def _eslint_finding(item: tuple[str, dict[str, Any]]) -> Finding | None:
    rel_path, msg = item
    rule_id = msg.get("ruleId")
    if not rule_id:
        return None
    message = msg.get("message") or ""
    severity_code = msg.get("severity") or 1
    line = msg.get("line") or 1
    severity = "High" if severity_code == 2 else "Medium"
    remediation = f"See https://eslint.org/docs/rules/{rule_id}"

    return Finding(
        severity=severity,  # type: ignore[arg-type]
        confidence=_CONFIDENCE,
        source=_SOURCE,
        file=rel_path,
        line=line,
        finding=f"{rule_id}: {message}",
        remediation=remediation,
        category="lint",
    )
