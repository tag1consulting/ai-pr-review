"""Native Python implementation of the checkov analyzer.

Replaces analyzers/run-checkov.sh. Pre-filters IaC files using the same
content-sniff patterns, invokes checkov, normalises its JSON output (single
object or array), and converts failed_checks to Finding instances.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 80
_SOURCE = "checkov"
_TIMEOUT_SECS = 120
_DEFAULT_REMEDIATION = "See https://docs.prismacloud.io/en/enterprise-edition/policy-reference"

# k8s apiVersion pattern: v<N> or <group>/v<N>[alpha|beta<N>]
_K8S_API_VERSION_RE = re.compile(
    r"^\s*apiVersion:\s*([a-z0-9][-a-z0-9.]*/)?v\d+(alpha\d+|beta\d+)?\s*$",
    re.MULTILINE,
)
_K8S_KIND_RE = re.compile(r"^\s*kind:", re.MULTILINE)
_CFN_YAML_RE = re.compile(r"^\s*AWSTemplateFormatVersion:", re.MULTILINE)
_AZURE_SCHEMA_RE = re.compile(r"schema\.management\.azure\.com", re.MULTILINE)
_CFN_JSON_KEY_RE = re.compile(r'"AWSTemplateFormatVersion"\s*:', re.MULTILINE)
_AZURE_JSON_KEY_RE = re.compile(
    r'"\$schema"\s*:\s*"[^"]*schema\.management\.azure\.com', re.MULTILINE
)

# Checkov high-severity check_id prefixes
_HIGH_PREFIX_RE = re.compile(r"^(CKV2_|CKV_SECRET_)")


def _is_iac_file(path: str) -> bool:
    """Return True when path is an IaC file checkov should scan."""
    p = Path(path)
    suffix = p.suffix.lower()
    name = p.name

    if suffix in (".tf", ".tfvars"):
        return True

    # Dockerfile patterns
    if name == "Dockerfile" or name.startswith("Dockerfile.") or suffix == ".dockerfile":
        return True

    if suffix in (".yaml", ".yml"):
        try:
            content = p.read_text(errors="replace")
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: cannot read %s for IaC sniff: %s", path, exc)
            return False
        if _CFN_YAML_RE.search(content):
            return True
        if _AZURE_SCHEMA_RE.search(content):
            return True
        return bool(_K8S_API_VERSION_RE.search(content) and _K8S_KIND_RE.search(content))

    if suffix == ".json":
        try:
            content = p.read_text(errors="replace")
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: cannot read %s for IaC sniff: %s", path, exc)
            return False
        return bool(_CFN_JSON_KEY_RE.search(content) or _AZURE_JSON_KEY_RE.search(content))

    return False


def _run_checkov(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run checkov on changed IaC files and return Finding instances."""
    candidate_files = changed_files.terraform + changed_files.iac + changed_files.dockerfile
    target_files = [f for f in dict.fromkeys(candidate_files) if Path(f).is_file() and _is_iac_file(f)]
    if not target_files:
        if candidate_files:
            logger.info(
                "[ai-pr-review] INFO: checkov: %d candidate file(s) filtered out — none matched IaC sniff patterns.",
                len(dict.fromkeys(candidate_files)),
            )
        return []

    if not shutil.which("checkov"):
        logger.warning("[ai-pr-review] WARNING: checkov not found; skipping.")
        return []

    file_args: list[str] = []
    for f in target_files:
        file_args += ["--file", f]

    try:
        result = subprocess.run(
            ["checkov", *file_args, "--output", "json", "--quiet", "--compact"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: checkov timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: checkov failed to start: %s", exc)
        return []

    if result.returncode >= 2:
        logger.warning(
            "[ai-pr-review] WARNING: checkov exited %d; possible installation error. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: checkov produced non-JSON output: %s", exc)
        return []

    # Normalise: checkov may return a single object or an array (one per framework).
    if isinstance(raw, dict):
        frameworks: list[dict[str, object]] = [raw]
    elif isinstance(raw, list):
        frameworks = [item for item in raw if isinstance(item, dict)]
    else:
        logger.warning(
            "[ai-pr-review] WARNING: checkov produced unexpected output structure (%s); skipping. Preview: %s",
            type(raw).__name__, repr(raw)[:100],
        )
        return []

    findings: list[Finding] = []
    for framework in frameworks:
        results_block = framework.get("results") or {}
        if not isinstance(results_block, dict):
            continue
        failed = results_block.get("failed_checks") or []
        if not isinstance(failed, list):
            continue
        for item in failed:
            if not isinstance(item, dict):
                continue
            check_id = item.get("check_id") or ""
            check_name = item.get("check_id_name") or item.get("resource") or "policy violation"
            repo_file = (item.get("repo_file_path") or "").lstrip("/")
            line_range = item.get("file_line_range") or []
            line = line_range[0] if line_range else 1
            guideline = item.get("guideline") or ""
            remediation = guideline if guideline else _DEFAULT_REMEDIATION
            severity = "High" if _HIGH_PREFIX_RE.match(check_id) else "Medium"
            category = "secret" if check_id.startswith("CKV_SECRET_") else "lint"

            try:
                findings.append(
                    Finding(
                        severity=severity,  # type: ignore[arg-type]
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=repo_file,
                        line=line,
                        finding=f"{check_id}: {check_name}",
                        remediation=remediation,
                        category=category,  # type: ignore[arg-type]
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: checkov dropped malformed finding: %s; item=%r",
                    exc, repr(item)[:200],
                )

    return findings
