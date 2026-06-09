"""Native Python implementation of the cve-check analyzer.

Replaces analyzers/run-cve-check.sh. Parses dependency manifests (go.mod,
package.json, requirements*.txt, composer.json), queries OSV.dev in a single
batch request, scores vulnerabilities via CVSS v3.1, and returns Finding
instances.

Source tag exception: source="osv" (not "cve-check"), and every finding
carries agent="dependency-check".
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import NamedTuple

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_SOURCE = "osv"
_AGENT = "dependency-check"
_OSV_API_URL = "https://api.osv.dev/v1/querybatch"
_HTTP_TIMEOUT = 10.0
_BATCH_SIZE = 1000

# Requirements.txt: only exact-pinned versions (== or ===)
_REQ_EXACT_RE = re.compile(r"===?")
# Composer: skip platform requirements
_COMPOSER_SKIP_RE = re.compile(r"^(php|ext-|lib-)", re.IGNORECASE)
# Queryable version: must start with a digit
_DIGIT_START_RE = re.compile(r"^\d")
_STRIP_PREFIX_RE = re.compile(r"^[\^~>=v<]+")
_STRIP_SUFFIX_RE = re.compile(r"[ ,<>=|&].*")


class Package(NamedTuple):
    ecosystem: str
    name: str
    version: str
    file: str
    tag: str  # "prod" | "dev" | ""


def _parse_go_mod(content: str, file_path: str) -> list[Package]:
    """Parse go.mod and return packages, applying replace directives."""
    lines = content.splitlines()

    # Two-pass: first collect replace map, then emit requires
    replaces: dict[str, tuple[str, str]] = {}
    skips: set[str] = set()

    in_replace = False
    for line in lines:
        line = line.split("//")[0].strip()
        if not line:
            continue
        if re.match(r"^replace\s*\(", line):
            in_replace = True
            continue
        if in_replace and line == ")":
            in_replace = False
            continue
        is_replace = in_replace or line.startswith("replace ")
        if is_replace and "=>" in line:
                left, _, right = line.partition("=>")
                left = left.strip()
                right = right.strip()
                left_parts = left.split()
                old_name = left_parts[-1] if left_parts else ""
                right_parts = right.split()
                if right_parts and re.match(r"^[./]", right_parts[0]):
                    if old_name:
                        skips.add(old_name)
                elif len(right_parts) >= 2 and re.match(r"^v", right_parts[1]):
                    new_name = right_parts[0]
                    new_ver = right_parts[1].lstrip("v")
                    if old_name:
                        replaces[old_name] = (new_name, new_ver)

    packages: list[Package] = []
    in_block = False
    for line in lines:
        line = line.split("//")[0].strip()
        if not line:
            continue
        if re.match(r"^require\s*\(", line):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if line.startswith("replace"):
            continue

        parts = line.split()
        if in_block and len(parts) >= 2 and parts[1].startswith("v"):
            name, ver = parts[0], parts[1].lstrip("v")
        elif not in_block and parts and parts[0] == "require" and len(parts) >= 3 and parts[2].startswith("v"):
            name, ver = parts[1], parts[2].lstrip("v")
        else:
            continue

        if name in skips:
            continue
        if name in replaces:
            name, ver = replaces[name]
        if not _DIGIT_START_RE.match(ver):
            continue
        packages.append(Package("Go", name, ver, file_path, ""))

    return packages


def _parse_package_json(content: str, file_path: str) -> list[Package]:
    """Parse package.json dependencies and devDependencies."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to parse %s: %s", file_path, exc)
        return []
    if not isinstance(data, dict):
        return []

    packages: list[Package] = []
    for section, tag in (("dependencies", "prod"), ("devDependencies", "dev")):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, raw_ver in deps.items():
            if not isinstance(raw_ver, str):
                continue
            ver = _STRIP_SUFFIX_RE.sub("", _STRIP_PREFIX_RE.sub("", raw_ver))
            if not _DIGIT_START_RE.match(ver):
                continue
            packages.append(Package("npm", name, ver, file_path, tag))

    return packages


def _parse_requirements_txt(content: str, file_path: str) -> list[Package]:
    """Parse requirements.txt for ==/*===*-pinned packages only."""
    packages: list[Package] = []
    for raw_line in content.splitlines():
        line = raw_line.split("#")[0].strip()
        if not line:
            continue
        if not _REQ_EXACT_RE.search(line):
            continue
        # Strip extras: requests[security]>=1.0 -> requests
        pkg_part = re.split(r"[>=<!~\[]", line)[0].strip()
        if not pkg_part:
            continue
        ver_match = re.search(r"===?\s*([0-9][^,;\s]*)", line)
        if not ver_match:
            continue
        ver = ver_match.group(1)
        if not _DIGIT_START_RE.match(ver):
            continue
        packages.append(Package("PyPI", pkg_part, ver, file_path, ""))

    return packages


def _parse_composer_json(content: str, file_path: str) -> list[Package]:
    """Parse composer.json require and require-dev sections."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to parse %s: %s", file_path, exc)
        return []
    if not isinstance(data, dict):
        return []

    packages: list[Package] = []
    combined: dict[str, object] = {}
    combined.update(data.get("require") or {})
    combined.update(data.get("require-dev") or {})
    for name, raw_ver in combined.items():
        if _COMPOSER_SKIP_RE.match(name):
            continue
        if not isinstance(raw_ver, str):
            continue
        ver = _STRIP_SUFFIX_RE.sub("", _STRIP_PREFIX_RE.sub("", raw_ver))
        if not _DIGIT_START_RE.match(ver):
            continue
        packages.append(Package("Packagist", name, ver, file_path, ""))

    return packages


def _parse_manifests(manifest_files: list[str]) -> list[Package]:
    """Parse all changed manifest files and return packages."""
    packages: list[Package] = []
    for path_str in manifest_files:
        p = Path(path_str)
        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: cannot read %s: %s", path_str, exc)
            continue
        base = p.name
        if base == "go.mod":
            packages.extend(_parse_go_mod(content, path_str))
        elif base == "package.json":
            packages.extend(_parse_package_json(content, path_str))
        elif base == "composer.json":
            packages.extend(_parse_composer_json(content, path_str))
        elif re.match(r"requirements.*\.txt$", base):
            packages.extend(_parse_requirements_txt(content, path_str))

    return packages


def _query_osv_batch(packages: list[Package]) -> list[dict[str, object]]:
    """POST all packages to OSV /v1/querybatch; return per-package results list."""
    queries = [
        {"version": pkg.version, "package": {"name": pkg.name, "ecosystem": pkg.ecosystem}}
        for pkg in packages
    ]
    try:
        response = httpx.post(
            _OSV_API_URL,
            json={"queries": queries},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        logger.warning("[ai-pr-review] WARNING: OSV API timed out: %s; skipping.", exc)
        return []
    except httpx.HTTPError as exc:
        logger.warning("[ai-pr-review] WARNING: OSV API request failed: %s; skipping.", exc)
        return []
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[ai-pr-review] WARNING: OSV API returned invalid JSON: %s", exc)
        return []

    results = data.get("results") or []
    if not isinstance(results, list):
        logger.warning("[ai-pr-review] WARNING: OSV batch response missing 'results' list.")
        return []
    if len(results) != len(packages):
        logger.warning(
            "[ai-pr-review] WARNING: OSV batch returned %d results for %d queries (index mismatch).",
            len(results), len(packages),
        )
    return [r if isinstance(r, dict) else {} for r in results]


def _cvss_v3_score(vector: str) -> float | None:
    """Compute CVSS v3.1 base score from a vector string. Returns None on parse failure."""
    # Parse key:value pairs after the version prefix
    parts = vector.split("/")
    metrics: dict[str, str] = {}
    for part in parts[1:]:
        if ":" in part:
            k, _, v = part.partition(":")
            metrics[k] = v

    required = {"AV", "AC", "PR", "UI", "S", "C", "I", "A"}
    if not required.issubset(metrics):
        return None

    av_map = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}
    ac_map = {"L": 0.77, "H": 0.44}
    ui_map = {"N": 0.85, "R": 0.62}
    cia_map = {"H": 0.56, "L": 0.22, "N": 0.0}
    pr_map_uc = {"N": 0.85, "L": 0.62, "H": 0.27}
    pr_map_c = {"N": 0.85, "L": 0.68, "H": 0.50}

    scope_changed = metrics.get("S") == "C"
    av = av_map.get(metrics.get("AV", ""))
    ac = ac_map.get(metrics.get("AC", ""))
    ui = ui_map.get(metrics.get("UI", ""))
    c = cia_map.get(metrics.get("C", ""))
    i = cia_map.get(metrics.get("I", ""))
    a = cia_map.get(metrics.get("A", ""))
    pr_map = pr_map_c if scope_changed else pr_map_uc
    pr = pr_map.get(metrics.get("PR", ""))

    if any(v is None for v in (av, ac, ui, c, i, a, pr)):
        return None

    iss = 1 - (1 - c) * (1 - i) * (1 - a)  # type: ignore[operator]
    impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15) if scope_changed else 6.42 * iss
    exploit = 8.22 * av * ac * pr * ui  # type: ignore[operator]

    if impact <= 0:
        return 0.0

    raw = min(1.08 * (impact + exploit), 10.0) if scope_changed else min(impact + exploit, 10.0)

    # Round up to one decimal per CVSS v3.1 spec
    return math.ceil(raw * 10) / 10


def _parse_cvss_score(score_str: str) -> float | None:
    """Extract numeric CVSS score from a string. Returns None for unparseable input."""
    if not isinstance(score_str, str):
        return None
    if re.match(r"^CVSS:3", score_str, re.IGNORECASE):
        return _cvss_v3_score(score_str)
    if re.match(r"^CVSS:[24]", score_str, re.IGNORECASE):
        # CVSS v4 formula too complex; v2 is deprecated. Fail safe.
        return None
    # Bare numeric "(9.8)" form
    m = re.search(r"\(([0-9]+(?:\.[0-9]+)?)\)", score_str)
    if m:
        return float(m.group(1))
    if re.match(r"^[0-9]+(?:\.[0-9]+)?$", score_str):
        return float(score_str)
    return None


def _pick_cvss(vuln: dict[str, object]) -> float | None:
    """Return highest parseable CVSS score from a vulnerability entry."""
    severities = vuln.get("severity") or []
    if not isinstance(severities, list):
        return None
    scores = []
    for s in severities:
        if not isinstance(s, dict):
            continue
        raw = s.get("score")
        parsed = _parse_cvss_score(str(raw)) if raw is not None else None
        if parsed is not None:
            scores.append(parsed)
    return max(scores) if scores else None


def _cvss_to_severity(score: float | None) -> tuple[str, int]:
    """Return (severity_label, confidence) for a CVSS score or None."""
    if score is None:
        return "High", 70  # fail-safe; heuristic-only
    if score >= 9.0:
        return "Critical", 90
    if score >= 7.0:
        return "High", 90
    if score >= 4.0:
        return "Medium", 90
    return "Low", 90


def _find_fixed_version(vuln: dict[str, object], pkg_name: str, ecosystem: str) -> str:
    """Extract the first fixed version from affected[] for the queried package."""
    affected = vuln.get("affected") or []
    if not isinstance(affected, list):
        return "unknown"

    candidates: list[str] = []
    for entry in affected:
        if not isinstance(entry, dict):
            continue
        pkg = entry.get("package") or {}
        if not isinstance(pkg, dict):
            continue
        is_match = pkg.get("name") == pkg_name and (pkg.get("ecosystem") or "") == ecosystem
        for r in (entry.get("ranges") or []):
            if not isinstance(r, dict):
                continue
            for event in (r.get("events") or []):
                if not isinstance(event, dict):
                    continue
                fixed = event.get("fixed")
                if fixed:
                    if is_match:
                        return str(fixed)
                    candidates.append(str(fixed))

    return candidates[0] if candidates else "unknown"


def _cve_id(vuln: dict[str, object]) -> str:
    """Return the first CVE alias, or the OSV id if none."""
    aliases = vuln.get("aliases") or []
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias.startswith("CVE-"):
                return alias
    return str(vuln.get("id") or "unknown")


def _line_for_package(file_path: str, pkg_name: str) -> int:
    """Best-effort: find line number of package declaration in manifest file."""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    base = Path(file_path).name
    escaped = re.escape(pkg_name)
    if base in ("package.json", "composer.json"):
        pattern = re.compile(rf'"{escaped}"\s*:')
    elif base == "go.mod":
        pattern = re.compile(rf"\s{escaped}\s")
    elif re.match(r"requirements.*\.txt$", base):
        pattern = re.compile(rf"^\s*{escaped}\s*[>=<!~\[]", re.MULTILINE)
    else:
        pattern = re.compile(re.escape(pkg_name))
    m = pattern.search(content)
    if m:
        return content[: m.start()].count("\n") + 1
    return 1


def _vulns_to_findings(pkg: Package, vulns: list[object]) -> list[Finding]:
    """Convert OSV vulnerability entries for a package into Finding instances."""
    findings: list[Finding] = []
    line = _line_for_package(pkg.file, pkg.name)

    for raw_vuln in vulns:
        if not isinstance(raw_vuln, dict):
            continue
        score = _pick_cvss(raw_vuln)
        severity, confidence = _cvss_to_severity(score)
        fixed = _find_fixed_version(raw_vuln, pkg.name, pkg.ecosystem)
        cve = _cve_id(raw_vuln)
        osv_id = str(raw_vuln.get("id") or cve)
        summary = str(raw_vuln.get("summary") or raw_vuln.get("details") or "Known vulnerability")

        score_display = f"CVSS {round(score * 10) / 10}" if score is not None else "CVSS:unknown"
        dev_suffix = " (dev dependency)" if pkg.tag == "dev" else ""
        finding_text = (
            f"{cve} ({osv_id}) [{score_display}]: {summary}. "
            f"Affects {pkg.name}@{pkg.version}{dev_suffix}."
        )
        if fixed == "unknown":
            remediation = (
                f"No fixed version published yet. Monitor "
                f"https://osv.dev/vulnerability/{osv_id} for mitigations and workarounds."
            )
        else:
            remediation = (
                f"Upgrade {pkg.name} to {fixed} or later. "
                f"See https://osv.dev/vulnerability/{osv_id}"
            )

        try:
            findings.append(
                Finding(
                    severity=severity,  # type: ignore[arg-type]
                    confidence=confidence,
                    source=_SOURCE,
                    agent=_AGENT,
                    file=pkg.file,
                    line=line,
                    finding=finding_text,
                    remediation=remediation,
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: cve-check dropped malformed finding: %s; vuln=%r",
                exc, repr(raw_vuln)[:200],
            )

    return findings


def _run_cve_check(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run OSV.dev CVE check on changed manifest files and return Finding instances."""
    manifest_files = [f for f in changed_files.manifest_lockfile if Path(f).is_file()]
    if not manifest_files:
        return []

    packages = _parse_manifests(manifest_files)
    if not packages:
        return []

    all_findings: list[Finding] = []

    for batch_start in range(0, len(packages), _BATCH_SIZE):
        batch = packages[batch_start: batch_start + _BATCH_SIZE]
        results = _query_osv_batch(batch)
        if not results:
            continue

        for idx, pkg in enumerate(batch):
            if idx >= len(results):
                break
            result = results[idx]
            vulns = result.get("vulns") or []
            if not isinstance(vulns, list) or not vulns:
                continue
            all_findings.extend(_vulns_to_findings(pkg, vulns))

    return all_findings
