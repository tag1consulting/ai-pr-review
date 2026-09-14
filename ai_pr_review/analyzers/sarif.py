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
import os
import time
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

    Workspace-root stripping: tools like Ruff emit absolute ``file://`` URIs
    rooted at the job's checkout directory.  On a GitHub-hosted runner that is
    ``/home/runner/work/<owner>/<repo>/``; a self-hosted runner can check out
    anywhere (a custom `_work` root, a persistent agent directory, etc.), and
    the hardcoded runner-path pattern below never matches those.
    ``GITHUB_WORKSPACE`` is set by the Actions runtime to the real checkout
    root on the host -- but this engine's ``container-action`` variant runs
    inside a container that remaps ``GITHUB_WORKSPACE`` to its own mount
    point (``/workspace``), which is unrelated to the host path baked into a
    SARIF file produced by a separate, non-containerized job (e.g. this
    repo's own ``sarif-prep`` job). So ``GITHUB_WORKSPACE`` alone is only
    reliable for the composite (non-container) action; ``AI_SARIF_HOST_WORKSPACE``
    is checked first when set, for exactly that container case (#846 review) --
    a caller can set it to the real host checkout root when it differs from
    the container's own ``GITHUB_WORKSPACE``. The ``runner/work/<owner>/<repo>/``
    regex remains as a last-resort fallback for when neither is set or
    matches (e.g. a SARIF file produced outside of Actions and fed in via the
    `sarif-paths` input). This is a SARIF/host-side normalization distinct
    from `analyzers/native/_paths.py::strip_workspace_prefix()`
    (container-side, used by native analyzer subprocess output) -- see issue
    #846.

    This stripping only ever applies to a path that was actually absolute in
    the original URI (a ``file://`` scheme, or an unqualified absolute path
    already rejected above) -- never to an already-relative URI, which would
    otherwise be misinterpreted as workspace-prefixed and truncated (#846
    review: e.g. a genuinely relative ``workspace/src/x.py`` losing its first
    segment when ``GITHUB_WORKSPACE`` happens to end in ``/workspace``).

    A path that stays absolute after all three strip attempts (none
    matched) has its leading ``/`` restored before being returned, rather
    than silently handed back as a relative-looking string -- so that
    `findings/scope.py`'s absolute-path tripwire still catches it instead of
    the failure going unnoticed a second time (#846 review).
    """
    import re as _re

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

    # Tracks whether *uri* represented an absolute filesystem path -- only
    # such a path is a candidate for workspace-prefix stripping below. A
    # bare (no-scheme) relative URI never sets this and is returned as-is
    # once past the traversal check (#846 review).
    was_absolute = False

    # For file:// URIs, urlparse leaves a single leading slash on the path
    # (file:///x → "/x").  Strip exactly one — lstrip("/") would also accept
    # "file:////etc/passwd" → "etc/passwd", bypassing the absolute-path check.
    # An absolute path with no scheme (e.g. /etc/passwd) is rejected outright.
    if parsed.scheme == "file":
        was_absolute = True
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

    path_str = str(pp)
    if not was_absolute:
        # Already relative -- never a workspace-prefix candidate. Applying
        # the stripping below to a relative path would silently truncate it
        # whenever it happens to start with a segment that looks like a
        # workspace prefix (#846 review).
        return path_str

    # Strip the job's checkout-root prefix so repo-relative paths produced by
    # tools (Ruff, etc.) match the diff. Try, in order: AI_SARIF_HOST_WORKSPACE
    # (the host checkout root, for callers running inside a container whose
    # own GITHUB_WORKSPACE has been remapped -- see this function's
    # docstring), then GITHUB_WORKSPACE (correct for the composite,
    # non-container action), then the hardcoded GitHub-hosted-runner regex.
    for env_name in ("AI_SARIF_HOST_WORKSPACE", "GITHUB_WORKSPACE"):
        workspace = os.environ.get(env_name, "")
        if not workspace:
            continue
        workspace_prefix = workspace.strip("/") + "/"
        if path_str.startswith(workspace_prefix):
            return path_str[len(workspace_prefix):]

    # Fallback: home/runner/work/<owner>/<repo>/<rest> or runner/work/<owner>/<repo>/<rest>
    stripped = _re.sub(r"^(?:home/)?runner/work/[^/]+/[^/]+/", "", path_str)
    if stripped != path_str:
        return stripped

    # Nothing matched: this path is still really absolute under the hood,
    # just missing its leading slash from the earlier scheme-stripping step.
    # Restore it rather than handing back a relative-looking string, so
    # findings/scope.py's absolute-path tripwire still catches this case
    # instead of the normalization failure going unnoticed (#846 review).
    logger.warning(
        "SARIF: could not resolve %r to a repo-relative path (no workspace "
        "prefix matched); leaving it absolute so downstream scoping flags it",
        uri,
    )
    return "/" + path_str


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


def load_sarif_files(paths: list[str]) -> tuple[list[Finding], float]:
    """Parse all SARIF files in *paths* and return ``(findings, elapsed_seconds)``.

    ``elapsed_seconds`` is the wall-clock time for the full ingestion pass
    (0.0 if *paths* is empty).  Each unreadable or malformed file is logged as
    a WARNING and skipped (fail-soft).  Findings from all files flow through
    the same merge/dedup/suppress pipeline as native analyzer findings.
    """
    t0 = time.monotonic()
    all_findings: list[Finding] = []
    for path in paths:
        file_findings = _parse_sarif_file(path)
        logger.info("SARIF: %r → %d finding(s)", path, len(file_findings))
        all_findings.extend(file_findings)
    return all_findings, time.monotonic() - t0
