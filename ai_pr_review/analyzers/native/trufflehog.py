"""Native Python implementation of the trufflehog analyzer.

Replaces analyzers/run-trufflehog.sh. Scans changed files for secrets using
trufflehog filesystem mode, applies path-based allowlist from .trufflehog.yml,
and converts findings to Finding instances.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_SOURCE = "trufflehog"
_TIMEOUT_SECS = 120

# Test-file path pattern: unverified secrets in these paths are demoted to Low.
# Verified secrets are never demoted.
_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|fixtures?|testdata|test_data|mocks?|stubs?|fakes?|examples?|samples?)/|"
    r"_test\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$|\.bats$|^test_[^/]+\.[a-z]+$|(^|/)test_[^/]+\.[a-z]+$"
)


def _load_allowlist(workspace: Path) -> set[str]:
    """Read path allowlist from .trufflehog.yml in workspace root."""
    config_path = workspace / ".trufflehog.yml"
    if not config_path.is_file():
        return set()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to parse .trufflehog.yml: %s", exc)
        return set()
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to read .trufflehog.yml: %s", exc)
        return set()
    if not isinstance(data, dict):
        return set()
    allowlist = data.get("allowlist") or {}
    if not isinstance(allowlist, dict):
        return set()
    paths = allowlist.get("paths") or []
    if not isinstance(paths, list):
        return set()
    return {str(p) for p in paths if isinstance(p, str)}


def _classify(finding: dict[str, object], file_path: str) -> tuple[str, int]:
    """Return (severity, confidence) for a trufflehog finding."""
    verified = bool(finding.get("Verified"))
    if verified:
        return "Critical", 95
    is_test = bool(_TEST_FILE_RE.search(file_path))
    if is_test:
        return "Low", 40
    return "High", 85


def _extract_file(finding: dict[str, object]) -> str:
    """Extract file path from SourceMetadata."""
    meta = finding.get("SourceMetadata") or {}
    if not isinstance(meta, dict):
        return "unknown"
    data = meta.get("Data") or {}
    if not isinstance(data, dict):
        return "unknown"
    # Filesystem scan
    fs = data.get("Filesystem") or {}
    if isinstance(fs, dict) and fs.get("file"):
        return str(fs["file"])
    # Git scan
    git = data.get("Git") or {}
    if isinstance(git, dict) and git.get("file"):
        return str(git["file"])
    return "unknown"


def _extract_line(finding: dict[str, object]) -> int:
    """Extract line number from SourceMetadata."""
    meta = finding.get("SourceMetadata") or {}
    if not isinstance(meta, dict):
        return 0
    data = meta.get("Data") or {}
    if not isinstance(data, dict):
        return 0
    for source_type in ("Filesystem", "Git"):
        src = data.get(source_type) or {}
        if isinstance(src, dict) and src.get("line") is not None:
            try:
                return int(src["line"])
            except (TypeError, ValueError):
                return 0
    return 0


def _run_trufflehog(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run trufflehog on changed files and return Finding instances."""
    target_files = [f for f in changed_files.all_files if Path(f).is_file()]
    if not target_files:
        return []

    if not shutil.which("trufflehog"):
        logger.warning("[ai-pr-review] WARNING: trufflehog not found; skipping.")
        return []

    workspace = diff_file.parent
    allowlist = _load_allowlist(workspace)

    config_args: list[str] = []
    if (workspace / ".trufflehog.yml").is_file():
        config_args = ["--config", str(workspace / ".trufflehog.yml")]

    cmd = ["trufflehog", "filesystem", "--json", "--no-update"] + config_args + target_files
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: trufflehog timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: trufflehog failed to start: %s", exc)
        return []

    if result.returncode not in (0, 1, 183):
        logger.warning(
            "[ai-pr-review] WARNING: trufflehog exited %d; findings may be incomplete. stderr: %s",
            result.returncode, result.stderr[:200],
        )

    if not result.stdout.strip():
        if result.returncode == 183:
            logger.info(
                "[ai-pr-review] INFO: trufflehog exited 183 (findings detected) but stdout was empty."
            )
        return []

    findings: list[Finding] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue

        file_path = _extract_file(item)
        if file_path in allowlist:
            continue

        detector_name = str(item.get("DetectorName") or "")
        verified = bool(item.get("Verified"))
        severity, confidence = _classify(item, file_path)
        line_no = _extract_line(item)
        verified_label = "verified" if verified else "unverified"
        is_test = severity == "Low" and not verified

        finding_text = (
            f"Potential secret detected: {detector_name} ({verified_label})"
            + (" [test file — likely mock data]" if is_test else "")
        )
        if is_test:
            remediation = (
                "Verify this is intentional test/mock data. "
                "If it is a real credential, rotate it immediately."
            )
        else:
            remediation = "Rotate the credential immediately and remove it from the repository history."

        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=confidence,
                    source=_SOURCE,
                    file=file_path,
                    line=line_no,
                    finding=finding_text,
                    remediation=remediation,
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: trufflehog dropped malformed finding: %s; "
                "file=%r line=%r severity=%r",
                exc, file_path, line_no, severity,
            )

    return findings
