"""Native Python implementation of the semgrep analyzer.

Replaces analyzers/run-semgrep.sh. Invokes semgrep directly via subprocess
and converts its JSON output to Finding instances.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "semgrep"
_TIMEOUT_SECS = 120

# Substring fragments checked against check_id, in priority order, when
# metadata.category is absent or doesn't map to a known Category value.
# Semgrep rule IDs conventionally embed the vulnerability class (e.g.
# "python.lang.security.audit.subprocess-shell-true"), so this is real
# per-rule signal, not a guess.
_CHECK_ID_CATEGORY_HINTS: tuple[tuple[str, str], ...] = (
    ("sql-injection", "injection"),
    ("sqli", "injection"),
    ("command-injection", "injection"),
    ("path-traversal", "injection"),
    ("xss", "injection"),
    ("ssrf", "injection"),
    ("deserialization", "injection"),
    ("secret", "secret"),
    ("hardcoded", "secret"),
    ("credential", "secret"),
    ("auth", "authz"),
    ("access-control", "authz"),
    ("privilege", "authz"),
)

# semgrep's own metadata.category values, mapped onto this repo's taxonomy.
_METADATA_CATEGORY_MAP: dict[str, str] = {
    "security": "other",  # too broad on its own; check_id hints refine further below
    "correctness": "lint",
    "best-practice": "lint",
    "maintainability": "lint",
    "compatibility": "lint",
    "performance": "lint",
}

# Semgrep rule IDs are dot/dash/underscore-delimited components (e.g.
# "python.lang.security.audit.sql-injection"). Bare substring containment
# lets an unrelated rule name absorb a hint fragment as a false-positive
# substring — e.g. "sqli" (intended for sql-injection rules) matches inside
# "python.lang.sqlite-config", mis-tagging an unrelated rule as "injection".
# Anchor each fragment to a delimiter (or start/end of string) on both sides
# so it must appear as a complete token, not an arbitrary substring.
_DELIM = r"[.\-_]"


def _compile_hint_pattern(fragment: str) -> re.Pattern[str]:
    escaped = re.escape(fragment)
    return re.compile(rf"(?:^|{_DELIM}){escaped}(?:{_DELIM}|$)")


_COMPILED_HINTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (_compile_hint_pattern(fragment), category) for fragment, category in _CHECK_ID_CATEGORY_HINTS
)


def _map_category(check_id: str, metadata: dict[str, object]) -> str:
    """Map a semgrep finding to this repo's Category taxonomy.

    Checks check_id substrings first (specific, high-confidence vulnerability
    class signal embedded in semgrep's own rule naming convention), then
    falls back to metadata.category (broader, semgrep-assigned bucket).
    Returns "other" when neither source yields a confident mapping — this is
    the correct default for a multi-purpose tool with rulesets whose rule IDs
    and metadata don't always carry classifiable signal (e.g. community rules
    under --config=auto).
    """
    lower_id = check_id.lower()
    for pattern, category in _COMPILED_HINTS:
        if pattern.search(lower_id):
            return category

    raw_category = metadata.get("category")
    if isinstance(raw_category, str):
        mapped = _METADATA_CATEGORY_MAP.get(raw_category.lower())
        if mapped:
            return mapped

    return "other"


def _resolve_config() -> list[str]:
    """Return semgrep --config arguments in priority order.

    Priority:
    1. SEMGREP_RULES env var (explicit config string, e.g. "p/ci")
    2. .semgrep/ directory containing *.yml files
    3. semgrep.yml in the current directory
    4. --config=auto (network fetch fallback)
    """
    rules_env = os.environ.get("SEMGREP_RULES", "").strip()
    if rules_env:
        return ["--config", rules_env]

    semgrep_dir = Path(".semgrep")
    if semgrep_dir.is_dir():
        yml_files = sorted(semgrep_dir.glob("*.yml"))
        if yml_files:
            args: list[str] = []
            for yf in yml_files:
                # Use relative path (relative to CWD) consistent with bash wrapper.
                try:
                    rel = yf.relative_to(Path.cwd())
                    args += ["--config", str(rel)]
                except ValueError:
                    args += ["--config", str(yf)]
            return args

    if Path("semgrep.yml").is_file():
        return ["--config", "semgrep.yml"]

    return ["--config=auto"]


def _run_semgrep(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run semgrep on changed files and return Finding instances."""
    target_files = [f for f in changed_files.all_files if Path(f).is_file()]
    if not target_files:
        return []

    if not shutil.which("semgrep"):
        logger.warning("[ai-pr-review] WARNING: semgrep not found; skipping.")
        return []

    config_args = _resolve_config()

    try:
        result = subprocess.run(
            ["semgrep", "--json", "--quiet", *config_args, "--", *target_files],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: semgrep timed out after %ss; skipping.", exc.timeout)
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: semgrep failed to start: %s", exc)
        return []

    if result.returncode not in (0, 1):
        logger.warning(
            "[ai-pr-review] WARNING: semgrep exited %d (possible network/config error); skipping. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: semgrep produced no output — possible network failure or config error. stderr: %s",
            result.stderr[:200],
        )
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: semgrep produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: semgrep produced unexpected output structure; skipping.")
        return []

    results = data.get("results") or []
    if not isinstance(results, list):
        logger.warning("[ai-pr-review] WARNING: semgrep 'results' is not a list; skipping.")
        return []

    findings: list[Finding] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") or {}
        severity_raw = extra.get("severity", "")
        if severity_raw == "ERROR":
            severity = "High"
        elif severity_raw == "WARNING":
            severity = "Medium"
        else:
            severity = "Low"

        check_id = item.get("check_id") or ""
        message = extra.get("message") or ""
        metadata = extra.get("metadata") or {}
        references = metadata.get("references") or []
        ref = references[0] if references else None
        remediation = f"See {ref}" if ref else (f"Review the semgrep rule: {check_id}" if check_id else "No remediation available")
        category = _map_category(check_id, metadata)

        start = item.get("start") or {}

        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=_CONFIDENCE,
                    source=_SOURCE,
                    file=item.get("path") or "",
                    line=start.get("line") or None,
                    finding=f"{check_id}: {message}",
                    remediation=remediation,
                    category=category,  # type: ignore[arg-type]
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: semgrep dropped malformed finding: %s; item=%r",
                exc, repr(item)[:200],
            )

    return findings
