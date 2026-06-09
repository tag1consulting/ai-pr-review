"""Native Python implementation of the kube-linter analyzer.

Replaces analyzers/run-kube-linter.sh. Invokes kube-linter directly via
subprocess and converts its JSON output to Finding instances.
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

_CONFIDENCE = 85
_SOURCE = "kube-linter"
_TIMEOUT_SECS = 120
_SNIFF_LINES = 50


def _is_k8s_manifest(path: str) -> bool:
    """Return True if the file looks like a Kubernetes manifest (apiVersion + kind)."""
    p = Path(path)
    if p.suffix not in (".yaml", ".yml", ".json"):
        return False
    try:
        lines = p.read_text(errors="replace").splitlines()[:_SNIFF_LINES]
        content = "\n".join(lines)
        if p.suffix in (".yaml", ".yml"):
            return "apiVersion:" in content and "kind:" in content
        else:
            return '"apiVersion"' in content and '"kind"' in content
    except OSError:
        return False


def _run_kube_linter(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run kube-linter on changed Kubernetes manifests and return Finding instances."""
    candidates = [f for f in changed_files.iac if Path(f).is_file()]
    eligible = [f for f in candidates if _is_k8s_manifest(f)]
    if not eligible:
        return []

    if not shutil.which("kube-linter"):
        logger.warning("[ai-pr-review] WARNING: kube-linter not found; skipping.")
        return []

    try:
        result = subprocess.run(
            ["kube-linter", "lint", "--format", "json", "--", *eligible],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: kube-linter timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: kube-linter failed to start: %s", exc)
        return []

    # kube-linter exits 1 when violations are found.
    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: kube-linter exited %d; skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: kube-linter produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: kube-linter produced unexpected output structure; skipping.")
        return []

    reports = data.get("Reports") or []
    if not isinstance(reports, list):
        return []

    findings: list[Finding] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        obj = report.get("Object") or {}
        metadata = obj.get("Metadata") or {}
        obj_type = obj.get("Type") or {}
        check = report.get("Check") or ""
        message = (report.get("Diagnostic") or {}).get("Message") or "policy violation"
        kind = obj_type.get("Kind") or "resource"
        name = obj.get("Name") or ""

        try:
            findings.append(
                Finding(
                    severity="Medium",
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=metadata.get("FilePath") or "unknown",
                    line=metadata.get("LineNumber") or None,
                    finding=f"{check}: {message} [{kind} {name}]",
                    remediation=report.get("Remediation") or "See https://docs.kubelinter.io/#/generated/checks",
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: kube-linter dropped malformed finding: %s; report=%r",
                exc, repr(report)[:200],
            )

    return findings
