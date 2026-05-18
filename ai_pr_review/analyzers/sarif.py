"""SARIF 2.1.0 ingestor — E3.S5.

Parses SARIF 2.1.0 files and converts each result to the typed ``Finding``
model.  Flows through the same dedup/suppress/post pipeline as native analyzer
findings.  Gated by ``AI_SARIF_PATHS`` / ``sarif-paths`` action input.

Severity mapping:
  error   → High
  warning → Medium
  note    → Low
  none    → Low  (SARIF "informational" level)

Confidence defaults to 90 for all SARIF findings.
Source tag: ``sarif:<runs[].tool.driver.name>``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from ai_pr_review.findings.models import Finding

logger = logging.getLogger(__name__)

_SARIF_SEVERITY_MAP: dict[str, str] = {
    "error": "High",
    "warning": "Medium",
    "note": "Low",
    "none": "Low",
}
_DEFAULT_CONFIDENCE = 90
_DEFAULT_SEVERITY = "Medium"


def _sanitize_sarif_path(uri: str) -> str:
    """Normalize a SARIF artifactLocation URI to a safe repo-relative path.

    Rejects (returns ``""``) anything that:
      - is absolute (starts with ``/`` after scheme stripping)
      - contains ``..`` segments after normalization
      - resolves outside the workspace root

    Handles ``file:///path``, ``file://hostname/path``, percent-encoded URIs,
    and raw relative paths.
    """
    if not uri:
        return ""

    # Parse via urlparse so authority components are handled correctly
    # (file://hostname/path leaves "hostname" as netloc; we drop it).
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        # Unknown scheme (http://, etc.) — not a local file
        logger.warning("SARIF: rejecting non-file URI %r", uri)
        return ""
    path = unquote(parsed.path or uri)

    # For file:// URIs, urlparse leaves a single leading slash on the path
    # (file:///x → "/x").  Strip exactly one — lstrip("/") would also accept
    # "file:////etc/passwd" → "etc/passwd", bypassing the absolute-path check.
    # An absolute path with no scheme (e.g. /etc/passwd) is rejected outright.
    if parsed.scheme == "file":
        path = path.removeprefix("/")
        # A remaining leading slash means the original URI was an attempt to
        # smuggle an absolute path through extra slashes — reject it.
        if path.startswith("/"):
            logger.warning("SARIF: rejecting file URI with extra leading slashes: %r", uri)
            return ""
    elif path.startswith("/"):
        logger.warning("SARIF: rejecting absolute path %r", uri)
        return ""

    # Reject path traversal segments
    pp = PurePosixPath(path)
    if any(part == ".." for part in pp.parts):
        logger.warning("SARIF: rejecting path with '..' segments: %r", uri)
        return ""

    return str(pp)


def _parse_sarif_file(path: str) -> list[Finding]:
    """Parse a single SARIF 2.1.0 file into a list of Findings.

    Logs a WARNING and returns ``[]`` if the file is unreadable or malformed.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: SARIF: could not read file %r: %s", path, exc)
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: SARIF: invalid JSON in %r: %s", path, exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: SARIF: root of %r is not an object; skipping", path)
        return []

    runs = data.get("runs")
    if not isinstance(runs, list):
        logger.warning("[ai-pr-review] WARNING: SARIF: no 'runs' array in %r; skipping", path)
        return []

    findings: list[Finding] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        driver_name = (
            run.get("tool", {}).get("driver", {}).get("name", "unknown")
            if isinstance(run.get("tool"), dict)
            else "unknown"
        )
        source_tag = f"sarif:{driver_name}"

        results = run.get("results")
        if not isinstance(results, list):
            continue

        # Build a rule-id → help text map for remediation
        rules: dict[str, str] = {}
        for rule in run.get("tool", {}).get("driver", {}).get("rules", []):
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id", "")
            help_text = ""
            if isinstance(rule.get("help"), dict):
                help_text = rule["help"].get("text", "")
            if rule_id and help_text:
                rules[rule_id] = help_text

        for result in results:
            if not isinstance(result, dict):
                continue
            finding = _convert_result(result, source_tag, rules)
            if finding is not None:
                findings.append(finding)

    return findings


def _convert_result(
    result: dict[str, object],
    source_tag: str,
    rules: dict[str, str],
) -> Finding | None:
    """Convert one SARIF result dict to a Finding, or None if invalid."""
    # Message
    message_obj = result.get("message")
    if isinstance(message_obj, dict):
        message = str(message_obj.get("text", "") or message_obj.get("markdown", ""))
    else:
        message = str(message_obj or "")
    message = message.strip()
    if not message:
        return None

    # Severity
    level = str(result.get("level", "warning")).lower()
    severity = _SARIF_SEVERITY_MAP.get(level, _DEFAULT_SEVERITY)

    # Rule ID (used as part of the finding text for context)
    rule_id = str(result.get("ruleId", "") or "")

    finding_text = f"[{rule_id}] {message}" if rule_id else message

    # Location
    file_path = ""
    line: int | None = None
    locations = result.get("locations")
    if isinstance(locations, list) and locations:
        loc = locations[0]
        if isinstance(loc, dict):
            phys = loc.get("physicalLocation")
            if isinstance(phys, dict):
                artifact = phys.get("artifactLocation")
                if isinstance(artifact, dict):
                    uri = str(artifact.get("uri", "") or "")
                    file_path = _sanitize_sarif_path(uri)
                region = phys.get("region")
                if isinstance(region, dict):
                    start_line = region.get("startLine")
                    if isinstance(start_line, int) and start_line >= 1:
                        line = start_line

    # Remediation from rule help
    remediation = rules.get(rule_id, "")

    try:
        return Finding(
            severity=severity,  # type: ignore[arg-type]
            confidence=_DEFAULT_CONFIDENCE,
            finding=finding_text,
            source=source_tag,
            file=file_path,
            line=line,
            remediation=remediation,
        )
    except (ValueError, TypeError) as exc:
        # ValueError covers pydantic validator failures; TypeError covers
        # genuine arg mismatches (which would indicate a Finding refactor
        # the SARIF parser hasn't caught up with — log loudly).
        # Include a truncated repr of the offending result dict so the
        # failure is reproducible without the original SARIF file.
        logger.warning(
            "SARIF: could not construct Finding from %r: %s (%s); result=%r",
            source_tag, exc, type(exc).__name__, repr(result)[:300],
        )
        return None


def load_sarif_files(paths: list[str]) -> list[Finding]:
    """Parse all SARIF files in *paths* and return a combined list of Findings.

    Each unreadable or malformed file is logged as a WARNING and skipped
    (fail-soft).  Findings from all files flow through the same
    merge/dedup/suppress pipeline as native analyzer findings.
    """
    all_findings: list[Finding] = []
    for path in paths:
        file_findings = _parse_sarif_file(path)
        logger.info("SARIF: %r → %d finding(s)", path, len(file_findings))
        all_findings.extend(file_findings)
    return all_findings
