"""Native Python implementation of the cve-check analyzer.

Replaces analyzers/run-cve-check.sh. Parses dependency manifests and lockfiles,
queries OSV.dev in a single batch request, scores vulnerabilities via CVSS v3.1,
and returns Finding instances.

Supported sources (lockfiles preferred over range manifests when both are present):
  npm:        package-lock.json (v1/v2/v3), yarn.lock (v1), pnpm-lock.yaml (v5/v6/v9)
  Python:     poetry.lock, Pipfile.lock, uv.lock, requirements*.txt (exact pins only)
  PHP:        composer.lock, composer.json
  Rust:       Cargo.lock (v1/v2/v3)
  Ruby:       Gemfile.lock
  Go:         go.mod (no lockfile; go.sum contains hashes, not usable versions)

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
# Intentionally named _HTTP_TIMEOUT rather than the _TIMEOUT_SECS convention
# used by the 12 subprocess-bound sibling analyzers.  Those time out a local
# CLI process (120 s); this times out an httpx HTTP request to the OSV API
# (10 s) — a different resource type with a much tighter budget.  Keeping the
# distinct name avoids someone "aligning" the value to 120 s inappropriately.
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


def _parse_package_lock_json(content: str, file_path: str) -> list[Package]:
    """Parse package-lock.json (lockfileVersion 1, 2, or 3) for exact versions."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to parse %s: %s", file_path, exc)
        return []
    if not isinstance(data, dict):
        return []

    packages: list[Package] = []

    # v2/v3: packages dict (keys are "node_modules/<name>" or "")
    pkgs = data.get("packages")
    if isinstance(pkgs, dict):
        for key, entry in pkgs.items():
            if not key or not isinstance(entry, dict):
                continue
            # Strip "node_modules/" prefix (possibly nested: "node_modules/a/node_modules/b")
            name = re.sub(r"^(node_modules/)+", "", key)
            if not name:
                continue
            ver = entry.get("version")
            if not isinstance(ver, str) or not _DIGIT_START_RE.match(ver):
                continue
            tag = "dev" if entry.get("dev") else "prod"
            packages.append(Package("npm", name, ver, file_path, tag))
        return packages

    # v1: dependencies dict (flat, with nested dependencies)
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        _collect_v1_deps(deps, file_path, packages)

    return packages


def _collect_v1_deps(deps: dict[str, object], file_path: str, out: list[Package]) -> None:
    """Recursively collect packages from npm lockfile v1 dependencies."""
    for name, entry in deps.items():
        if not isinstance(entry, dict):
            continue
        ver = entry.get("version")
        if isinstance(ver, str) and _DIGIT_START_RE.match(ver):
            tag = "dev" if entry.get("dev") else "prod"
            out.append(Package("npm", name, ver, file_path, tag))
        nested = entry.get("dependencies")
        if isinstance(nested, dict):
            _collect_v1_deps(nested, file_path, out)


def _parse_yarn_lock(content: str, file_path: str) -> list[Package]:
    """Parse yarn.lock v1 (classic) format for exact resolved versions."""
    packages: list[Package] = []
    # Each stanza: one or more "name@range, name@range2:" header lines, then "  version \"x.y.z\""
    current_names: list[str] = []
    version: str | None = None

    for line in content.splitlines():
        # Skip comments and blank lines
        if not line or line.startswith("#"):
            if current_names and version:
                for name in current_names:
                    if _DIGIT_START_RE.match(version):
                        packages.append(Package("npm", name, version, file_path, ""))
            current_names = []
            version = None
            continue

        # Stanza header: "lodash@^4.17.0, lodash@^4.17.1:"
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            if current_names and version:
                for name in current_names:
                    if _DIGIT_START_RE.match(version):
                        packages.append(Package("npm", name, version, file_path, ""))
            current_names = []
            version = None
            header = line.rstrip().rstrip(":")
            for spec in header.split(","):
                spec = spec.strip()
                # Extract package name: everything before the last "@" (handles scoped packages)
                at_idx = spec.rfind("@")
                if at_idx > 0:
                    current_names.append(spec[:at_idx])
            continue

        # Version line inside a stanza
        m = re.match(r'^\s+version\s+"([^"]+)"', line)
        if m:
            version = m.group(1)
            continue

    # Flush last stanza
    if current_names and version:
        for name in current_names:
            if _DIGIT_START_RE.match(version):
                packages.append(Package("npm", name, version, file_path, ""))

    # Deduplicate by (name, version) -- yarn.lock lists a package once per unique resolved version
    seen: set[tuple[str, str]] = set()
    result: list[Package] = []
    for p in packages:
        key = (p.name, p.version)
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def _parse_pnpm_lock_yaml(content: str, file_path: str) -> list[Package]:
    """Parse pnpm-lock.yaml (v5, v6, v9) for exact package versions.

    Avoids a PyYAML dependency by parsing the relevant lines directly.
    v5/v6: package keys are indented under "packages:" as "  /lodash@4.17.20:"
    v9:    package keys appear as "  lodash@4.17.20:" under "snapshots:" or "packages:"
    """
    packages: list[Package] = []
    seen: set[tuple[str, str]] = set()

    for line in content.splitlines():
        stripped = line.rstrip()
        # Package entry key lines end with ":"
        if not stripped.endswith(":"):
            continue
        # Must be indented by exactly 2 spaces (package sub-key, not a top-level section)
        if not stripped.startswith("  ") or stripped.startswith("   "):
            continue
        key = stripped.strip().rstrip(":").lstrip("/").strip("'\"")
        # key looks like "lodash@4.17.20" or "@babel/core@7.21.0"
        at_idx = key.rfind("@")
        if at_idx <= 0:
            continue
        name = key[:at_idx]
        ver = key[at_idx + 1:]
        # Strip optional peer suffix: "4.17.20(react@18.0.0)" -> "4.17.20"
        ver = re.split(r"[(_]", ver)[0]
        if not _DIGIT_START_RE.match(ver):
            continue
        pk = (name, ver)
        if pk not in seen:
            seen.add(pk)
            packages.append(Package("npm", name, ver, file_path, ""))

    return packages


def _parse_toml_package_lock(content: str, file_path: str, ecosystem: str) -> list[Package]:
    """Parse a TOML-like lockfile with [[package]] stanzas.

    Shared implementation for poetry.lock and uv.lock — both use the same
    [[package]] / name = "..." / version = "..." structure and resolve to
    the same PyPI ecosystem.
    """
    packages: list[Package] = []
    current_name: str | None = None
    current_version: str | None = None
    in_package = False

    for line in content.splitlines():
        line = line.strip()
        if line == "[[package]]":
            if current_name and current_version and _DIGIT_START_RE.match(current_version):
                packages.append(Package(ecosystem, current_name, current_version, file_path, ""))
            current_name = None
            current_version = None
            in_package = True
            continue
        if not in_package:
            continue
        m = re.match(r'^name\s*=\s*"([^"]+)"', line)
        if m:
            current_name = m.group(1)
            continue
        m = re.match(r'^version\s*=\s*"([^"]+)"', line)
        if m:
            current_version = m.group(1)
            continue

    # Flush last entry
    if current_name and current_version and _DIGIT_START_RE.match(current_version):
        packages.append(Package(ecosystem, current_name, current_version, file_path, ""))

    return packages


def _parse_poetry_lock(content: str, file_path: str) -> list[Package]:
    """Parse poetry.lock TOML-like format for exact package versions."""
    return _parse_toml_package_lock(content, file_path, "PyPI")


def _parse_pipfile_lock(content: str, file_path: str) -> list[Package]:
    """Parse Pipfile.lock JSON for exact pinned versions."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to parse %s: %s", file_path, exc)
        return []
    if not isinstance(data, dict):
        return []

    packages: list[Package] = []
    for section, tag in (("default", "prod"), ("develop", "dev")):
        section_data = data.get(section) or {}
        if not isinstance(section_data, dict):
            continue
        for name, entry in section_data.items():
            if not isinstance(entry, dict):
                continue
            raw_ver = entry.get("version")
            if not isinstance(raw_ver, str):
                continue
            # version field is "==1.2.3"
            ver = raw_ver.lstrip("=").strip()
            if not _DIGIT_START_RE.match(ver):
                continue
            packages.append(Package("PyPI", name, ver, file_path, tag))

    return packages


def _parse_uv_lock(content: str, file_path: str) -> list[Package]:
    """Parse uv.lock TOML-like format for exact package versions."""
    return _parse_toml_package_lock(content, file_path, "PyPI")


def _parse_composer_lock(content: str, file_path: str) -> list[Package]:
    """Parse composer.lock JSON for exact installed versions."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: failed to parse %s: %s", file_path, exc)
        return []
    if not isinstance(data, dict):
        return []

    packages: list[Package] = []
    for section in ("packages", "packages-dev"):
        entries = data.get(section) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            ver = entry.get("version")
            if not isinstance(name, str) or not isinstance(ver, str):
                continue
            # composer.lock versions often have "v" prefix: "v5.4.20" -> "5.4.20"
            ver = ver.lstrip("v")
            if not _DIGIT_START_RE.match(ver):
                continue
            packages.append(Package("Packagist", name, ver, file_path, ""))

    return packages


def _parse_cargo_lock(content: str, file_path: str) -> list[Package]:
    """Parse Cargo.lock TOML-like format for exact crate versions."""
    packages: list[Package] = []
    current_name: str | None = None
    current_version: str | None = None
    has_source = False
    in_package = False

    for line in content.splitlines():
        line = line.strip()
        if line == "[[package]]":
            # Only emit packages with a source (skip workspace members)
            if current_name and current_version and has_source and _DIGIT_START_RE.match(current_version):
                packages.append(Package("crates.io", current_name, current_version, file_path, ""))
            current_name = None
            current_version = None
            has_source = False
            in_package = True
            continue
        if not in_package:
            continue
        m = re.match(r'^name\s*=\s*"([^"]+)"', line)
        if m:
            current_name = m.group(1)
            continue
        m = re.match(r'^version\s*=\s*"([^"]+)"', line)
        if m:
            current_version = m.group(1)
            continue
        if line.startswith("source"):
            has_source = True

    if current_name and current_version and has_source and _DIGIT_START_RE.match(current_version):
        packages.append(Package("crates.io", current_name, current_version, file_path, ""))

    return packages


def _parse_gemfile_lock(content: str, file_path: str) -> list[Package]:
    """Parse Gemfile.lock for exact gem versions."""
    packages: list[Package] = []
    in_gem_section = False

    for line in content.splitlines():
        stripped = line.rstrip()
        # Section headers are unindented
        if not stripped.startswith(" "):
            in_gem_section = stripped in ("GEM", "GIT", "PATH")
            continue
        if not in_gem_section:
            continue
        # "  remote: ..." and "  specs:" are section metadata
        if stripped.lstrip().startswith("remote:") or stripped.lstrip() == "specs:":
            continue
        # Gem entries are indented with 4+ spaces: "    rails (7.0.4)"
        m = re.match(r"^    ([a-zA-Z0-9_\-\.]+)\s+\(([0-9][^)]*)\)", stripped)
        if m:
            name = m.group(1)
            ver = m.group(2).split("-")[0]  # strip platform suffix e.g. "1.2.3-x86_64-linux"
            if _DIGIT_START_RE.match(ver):
                packages.append(Package("RubyGems", name, ver, file_path, ""))

    # Deduplicate (same gem may appear under multiple sections)
    seen: set[tuple[str, str]] = set()
    result: list[Package] = []
    for p in packages:
        key = (p.name, p.version)
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


# Maps each lockfile basename to its parser and the range-manifest basenames it supersedes.
# When a lockfile is present, packages from the corresponding range manifests are dropped.
_LOCKFILE_SUPERSEDES: dict[str, frozenset[str]] = {
    "package-lock.json": frozenset({"package.json"}),
    "npm-shrinkwrap.json": frozenset({"package.json"}),
    "yarn.lock": frozenset({"package.json"}),
    "pnpm-lock.yaml": frozenset({"package.json"}),
    "poetry.lock": frozenset({"requirements.txt", "Pipfile", "pyproject.toml"}),
    "Pipfile.lock": frozenset({"requirements.txt", "Pipfile"}),
    "uv.lock": frozenset({"requirements.txt", "pyproject.toml"}),
    "composer.lock": frozenset({"composer.json"}),
    "Cargo.lock": frozenset({"Cargo.toml"}),
    "Gemfile.lock": frozenset({"Gemfile"}),
}


def _parse_manifests(manifest_files: list[str]) -> list[Package]:
    """Parse changed manifest/lockfile paths and return packages.

    Lockfiles are preferred over range manifests when both are present in the
    changed file list. For example, if both package.json and package-lock.json
    changed, only the lockfile's exact versions are queried -- the range
    manifest is skipped.
    """
    # Determine which range-manifest basenames are superseded by a present lockfile
    present_basenames = {Path(f).name for f in manifest_files}
    suppressed: set[str] = set()
    for lockfile_base, supersedes in _LOCKFILE_SUPERSEDES.items():
        if lockfile_base in present_basenames:
            suppressed.update(supersedes)

    packages: list[Package] = []
    for path_str in manifest_files:
        p = Path(path_str)
        if not p.is_file():
            continue
        base = p.name
        if base in suppressed:
            logger.debug(
                "[ai-pr-review] cve-check: skipping range manifest %s (lockfile present)", path_str
            )
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: cannot read %s: %s", path_str, exc)
            continue

        if base == "go.mod":
            packages.extend(_parse_go_mod(content, path_str))
        elif base in ("package-lock.json", "npm-shrinkwrap.json"):
            packages.extend(_parse_package_lock_json(content, path_str))
        elif base == "yarn.lock":
            packages.extend(_parse_yarn_lock(content, path_str))
        elif base == "pnpm-lock.yaml":
            packages.extend(_parse_pnpm_lock_yaml(content, path_str))
        elif base == "package.json":
            packages.extend(_parse_package_json(content, path_str))
        elif base == "poetry.lock":
            packages.extend(_parse_poetry_lock(content, path_str))
        elif base == "Pipfile.lock":
            packages.extend(_parse_pipfile_lock(content, path_str))
        elif base == "uv.lock":
            packages.extend(_parse_uv_lock(content, path_str))
        elif base == "composer.lock":
            packages.extend(_parse_composer_lock(content, path_str))
        elif base == "composer.json":
            packages.extend(_parse_composer_json(content, path_str))
        elif base == "Cargo.lock":
            packages.extend(_parse_cargo_lock(content, path_str))
        elif base == "Gemfile.lock":
            packages.extend(_parse_gemfile_lock(content, path_str))
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
            "[ai-pr-review] WARNING: OSV batch returned %d results for %d queries "
            "(index mismatch); truncating to avoid mis-attributed findings.",
            len(results), len(packages),
        )
        results = results[: len(packages)]
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
            result = results[idx] if idx < len(results) else {}

            vulns = result.get("vulns") or []
            if not isinstance(vulns, list) or not vulns:
                continue
            all_findings.extend(_vulns_to_findings(pkg, vulns))

    return all_findings
