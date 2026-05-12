"""Declarative suppression of findings via suppressions.json.

Ports lib/findings.sh apply_suppressions with:
- Bounded httpx timeouts (connect=5s, read=10s) — closes #187
- Max 1 retry on transient registry failure
- Unavailable registry → finding kept with WARNING logged
- Local suppressions from {workspace}/.github/ai-pr-review/suppressions.json
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ai_pr_review.findings.models import Finding

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_RETRIES = 1


@dataclass
class SuppressionRule:
    id: str
    reason: str
    match_file: str = ""
    match_pattern: str = ""
    verify: str = ""


def load_rules(
    script_dir: str,
    workspace: str = "",
) -> list[SuppressionRule]:
    """Load global + optional local suppression rules."""
    global_path = Path(script_dir) / "config" / "suppressions.json"
    local_path = (
        Path(workspace) / ".github" / "ai-pr-review" / "suppressions.json"
        if workspace
        else None
    )

    rules_data: list[dict[str, Any]] = []
    if global_path.is_file():
        try:
            rules_data = json.loads(global_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: Could not load suppressions.json: {exc}", file=sys.stderr)

    if local_path and local_path.is_file():
        try:
            local_data = json.loads(local_path.read_text())
            if isinstance(local_data, list):
                rules_data.extend(local_data)
                print(
                    "Loaded local suppressions from .github/ai-pr-review/suppressions.json",
                    file=sys.stderr,
                )
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: Could not load local suppressions: {exc}", file=sys.stderr)

    return [_parse_rule(r) for r in rules_data if isinstance(r, dict)]


def _parse_rule(raw: dict[str, Any]) -> SuppressionRule:
    match = raw.get("match", {})
    return SuppressionRule(
        id=raw.get("id", ""),
        reason=raw.get("reason", ""),
        match_file=match.get("file", ""),
        match_pattern=match.get("pattern", ""),
        verify=raw.get("verify", ""),
    )


def apply_suppressions(
    findings: list[Finding],
    rules: list[SuppressionRule],
) -> tuple[list[Finding], int]:
    """Return (kept_findings, suppressed_count).

    For rules with a verify field, confirm the version exists via the
    appropriate registry before accepting the suppression.
    """
    kept: list[Finding] = []
    suppressed_count = 0

    for finding in findings:
        matching_rule = _find_matching_rule(finding, rules)
        if matching_rule is None:
            kept.append(finding)
            continue

        if matching_rule.verify:
            if _verify_version(finding, matching_rule.verify):
                suppressed_count += 1
            else:
                # Registry unavailable or version not confirmed — keep finding
                kept.append(finding)
        else:
            suppressed_count += 1

    return kept, suppressed_count


def _find_matching_rule(
    finding: Finding,
    rules: list[SuppressionRule],
) -> SuppressionRule | None:
    for rule in rules:
        if not _rule_matches(finding, rule):
            continue
        return rule
    return None


def _rule_matches(finding: Finding, rule: SuppressionRule) -> bool:
    if rule.match_file:
        file_pat = re.compile(rule.match_file, re.IGNORECASE)
        if not file_pat.search(finding.file or ""):
            return False
    if rule.match_pattern:
        text_pat = re.compile(rule.match_pattern, re.IGNORECASE)
        combined = f"{finding.finding} {finding.remediation}"
        if not text_pat.search(combined):
            return False
    return True


def _verify_version(finding: Finding, verify_type: str) -> bool:
    """Confirm the version referenced in the finding exists in its registry.

    Returns True if confirmed (suppress), False if unconfirmed (keep).
    """
    text = f"{finding.finding} {finding.remediation}"
    try:
        if verify_type == "github-releases":
            return _verify_github_release(text)
        if verify_type == "npm":
            return _verify_npm(text)
        if verify_type == "pypi":
            return _verify_pypi(text)
        if verify_type == "go":
            return _verify_go(text)
        if verify_type == "cargo":
            return _verify_cargo(text)
        if verify_type == "docker-hub":
            return _verify_docker_hub(text)
        if verify_type == "ruby-org":
            return _verify_ruby(text)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: suppression verify ({verify_type}) error ({type(exc).__name__}); keeping finding. {exc}",
            file=sys.stderr,
        )
    return False


def _get(url: str) -> httpx.Response:
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return client.get(url)
            except httpx.TransportError:
                if attempt == _MAX_RETRIES:
                    raise
    raise RuntimeError("unreachable")


def _verify_github_release(text: str) -> bool:
    m = re.search(r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)@(v?[0-9][0-9a-zA-Z._-]*)", text)
    if not m:
        return False
    repo, tag = m.group(1), m.group(2)
    resp = _get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    if resp.status_code == 200:
        print("  Confirmed tag/release exists — suppressing finding.", file=sys.stderr)
        return True
    print(
        f"  WARNING: tag {tag} not found for {repo}; keeping finding.",
        file=sys.stderr,
    )
    return False


def _verify_npm(text: str) -> bool:
    m = re.search(r"@?([a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)?)@([0-9][0-9a-zA-Z._-]*)", text)
    if not m:
        return False
    pkg, ver = m.group(1), m.group(2)
    resp = _get(f"https://registry.npmjs.org/{pkg}/{ver}")
    return resp.status_code == 200


def _verify_pypi(text: str) -> bool:
    m = re.search(r"([a-zA-Z0-9_.-]+)==([0-9][0-9a-zA-Z._-]*)", text)
    if not m:
        return False
    pkg, ver = m.group(1), m.group(2)
    resp = _get(f"https://pypi.org/pypi/{pkg}/{ver}/json")
    return resp.status_code == 200


def _verify_go(text: str) -> bool:
    m = re.search(r"([a-zA-Z0-9._/-]+)@(v[0-9][0-9a-zA-Z._-]*)", text)
    if not m:
        return False
    mod, ver = m.group(1), m.group(2)
    resp = _get(f"https://proxy.golang.org/{mod}/@v/{ver}.info")
    return resp.status_code == 200


def _verify_cargo(text: str) -> bool:
    m = re.search(r"([a-zA-Z0-9_-]+)\s+([0-9][0-9a-zA-Z._-]*)", text)
    if not m:
        return False
    crate, ver = m.group(1), m.group(2)
    resp = _get(f"https://crates.io/api/v1/crates/{crate}/{ver}")
    return resp.status_code == 200


def _verify_docker_hub(text: str) -> bool:
    # Require an explicit tag (image:tag or org/image:tag) to avoid false matches.
    m = re.search(
        r"([a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)?):([a-zA-Z0-9._-]+)", text
    )
    if not m:
        return False
    image, tag = m.group(1), m.group(2)
    parts = image.split("/")
    if len(parts) == 1:
        url = f"https://hub.docker.com/v2/repositories/library/{parts[0]}/tags/{tag}/"
    else:
        url = f"https://hub.docker.com/v2/repositories/{image}/tags/{tag}/"
    resp = _get(url)
    return resp.status_code == 200


def _verify_ruby(text: str) -> bool:
    m = re.search(r"([a-zA-Z0-9_-]+)\s+([0-9][0-9a-zA-Z._-]*)", text)
    if not m:
        return False
    gem, ver = m.group(1), m.group(2)
    resp = _get(f"https://rubygems.org/api/v1/versions/{gem}.json")
    if resp.status_code != 200:
        return False
    versions = [v.get("number", "") for v in resp.json()]
    return ver in versions
