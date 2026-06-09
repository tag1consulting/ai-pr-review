"""Native Python implementation of the golangci-lint analyzer.

Replaces analyzers/run-golangci-lint.sh. Discovers the Go module root by
walking up from the first changed Go file, derives package patterns, invokes
golangci-lint, and converts its JSON output to Finding instances.
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
_SOURCE = "golangci-lint"
_TIMEOUT_SECS = 120

# Linters whose findings map to High severity; all others → Medium.
_HIGH_SEVERITY_LINTERS = frozenset({"errcheck", "govet", "staticcheck"})


def _find_module_root(go_files: list[str]) -> Path | None:
    """Walk up from the first Go file's directory to find go.mod."""
    candidate = Path(go_files[0]).resolve().parent
    while True:
        if (candidate / "go.mod").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def _pkg_patterns(go_files: list[str], module_root: Path) -> list[str]:
    """Return unique ./dir/... patterns relative to the module root."""
    seen: set[str] = set()
    patterns: list[str] = []
    for f in go_files:
        rel = Path(f).resolve().relative_to(module_root)
        pkg_dir = str(rel.parent)
        if pkg_dir not in seen:
            seen.add(pkg_dir)
            patterns.append(f"./{pkg_dir}/...")
    return patterns


def _run_golangci_lint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run golangci-lint on changed Go files and return Finding instances."""
    target_files = [f for f in changed_files.go if Path(f).is_file()]
    if not target_files:
        return []

    if not shutil.which("golangci-lint"):
        logger.warning("[ai-pr-review] WARNING: golangci-lint not found; skipping.")
        return []

    module_root = _find_module_root(target_files)
    if module_root is None:
        logger.warning("[ai-pr-review] WARNING: could not find go.mod; golangci-lint check skipped.")
        return []

    patterns = _pkg_patterns(target_files, module_root)

    try:
        result = subprocess.run(
            ["golangci-lint", "run", "--out-format=json", "--issues-exit-code=0", *patterns],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
            cwd=str(module_root),
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: golangci-lint timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: golangci-lint failed to start: %s", exc)
        return []

    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: golangci-lint exited %d; skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: golangci-lint produced no output. stderr: %s",
            result.stderr[:200],
        )
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: golangci-lint produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: golangci-lint produced unexpected output structure; skipping.")
        return []

    issues = data.get("Issues") or []
    if not isinstance(issues, list):
        logger.warning("[ai-pr-review] WARNING: golangci-lint 'Issues' is not a list; skipping.")
        return []

    # Prepend module root prefix when it is not the CWD itself.
    # golangci-lint reports Pos.Filename relative to the module root.
    resolved_cwd = Path(".").resolve()
    if module_root != resolved_cwd:
        try:
            prefix = str(module_root.relative_to(resolved_cwd)) + "/"
        except ValueError:
            prefix = str(module_root) + "/"
    else:
        prefix = ""

    findings: list[Finding] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        linter = item.get("FromLinter") or ""
        text = item.get("Text") or ""
        pos = item.get("Pos") or {}
        filename = pos.get("Filename") or ""
        line = pos.get("Line") or None

        severity = "High" if linter in _HIGH_SEVERITY_LINTERS else "Medium"

        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=prefix + filename,
                    line=line,
                    finding=f"{linter}: {text}",
                    remediation=f"Review the {linter} linter documentation for this issue.",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: golangci-lint dropped malformed finding: %s; item=%r",
                exc, repr(item)[:200],
            )

    return findings
