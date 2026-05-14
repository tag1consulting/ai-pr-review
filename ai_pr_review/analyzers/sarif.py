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
from pathlib import Path

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


def _parse_sarif_file(path: str) -> list[Finding]:
    """Parse a single SARIF 2.1.0 file into a list of Findings.

    Logs a WARNING and returns ``[]`` if the file is unreadable or malformed.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("SARIF: could not read file %r: %s", path, exc)
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("SARIF: invalid JSON in %r: %s", path, exc)
        return []

    if not isinstance(data, dict):
        logger.warning("SARIF: root of %r is not an object; skipping", path)
        return []

    runs = data.get("runs")
    if not isinstance(runs, list):
        logger.warning("SARIF: no 'runs' array in %r; skipping", path)
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
                    # Strip common URI prefixes
                    file_path = uri.removeprefix("file:///").removeprefix("file://")
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
    except Exception as exc:
        logger.warning("SARIF: could not construct Finding from result: %s", exc)
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
