"""Native Python implementation of the kube-linter analyzer.

Replaces analyzers/run-kube-linter.sh. Invokes kube-linter directly via
subprocess and converts its JSON output to Finding instances.
"""

from __future__ import annotations

import logging
import shutil

# subprocess is never called directly in this module (run_cli_json_analyzer
# owns the actual subprocess.run call) but stays imported: existing tests
# patch it as "ai_pr_review.analyzers.native.kube_linter.subprocess.run",
# which resolves the attribute on *this* module first. Since `subprocess` is
# a singleton module object, the patch still lands on the real subprocess.run
# that _cli_runner.py calls -- removing the import would only break the
# test's attribute lookup, not the patch's effect.
import subprocess  # noqa: F401
from pathlib import Path
from typing import Any, Literal

from ai_pr_review.analyzers.native._cli_runner import run_cli_json_analyzer
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 85
_SOURCE = "kube-linter"
_TIMEOUT_SECS = 120
_SNIFF_LINES = 50

# Checks with direct security impact map to High; all others → Medium.
_HIGH_SEVERITY_CHECKS = frozenset({
    "run-as-non-root",
    "privilege-escalation-container",
    "writable-host-mount",
    "no-read-only-root-fs",
    "sensitive-host-mounts",
    "dangerously-broad-host-path",
    "host-network",
    "host-pid",
    "host-ipc",
    "privileged-container",
    "drop-net-raw-capability",
    "no-seccomp-profile",
    "unsafe-proc-mount",
    "allow-privilege-escalation-unset",
    "run-as-non-root-user",
})


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

    return run_cli_json_analyzer(
        tool="kube-linter",
        command=["kube-linter", "lint", "--format", "json", "--", *eligible],
        timeout_secs=_TIMEOUT_SECS,
        # kube-linter exits 1 when violations are found -- the runner's
        # default success set ({0, 1}) already covers this.
        extract_items=_kube_linter_items,
        build_finding=_kube_linter_finding,
    )


def _kube_linter_items(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: kube-linter produced unexpected output structure; skipping.")
        return None

    if "Reports" not in data:
        logger.warning("[ai-pr-review] WARNING: kube-linter output missing 'Reports' key; skipping.")
        return None
    reports = data["Reports"] or []
    if not isinstance(reports, list):
        return None
    return [r for r in reports if isinstance(r, dict)]


def _kube_linter_finding(report: dict[str, Any]) -> Finding:
    obj = report.get("Object") or {}
    metadata = obj.get("Metadata") or {}
    obj_type = obj.get("Type") or {}
    check = report.get("Check") or ""
    message = (report.get("Diagnostic") or {}).get("Message") or "policy violation"
    kind = obj_type.get("Kind") or "resource"
    name = obj.get("Name") or ""

    severity: Literal["High", "Medium"] = "High" if check in _HIGH_SEVERITY_CHECKS else "Medium"
    category = "authz" if check in _HIGH_SEVERITY_CHECKS else "lint"
    return Finding(
        severity=severity,
        confidence=_CONFIDENCE,
        source=_SOURCE,
        file=metadata.get("FilePath") or "unknown",
        line=metadata.get("LineNumber") or None,
        finding=f"{check}: {message} [{kind} {name}]",
        remediation=report.get("Remediation") or "See https://docs.kubelinter.io/#/generated/checks",
        category=category,  # type: ignore[arg-type]
    )
