"""Native Python implementation of the checkov analyzer.

Replaces analyzers/run-checkov.sh. Pre-filters IaC files using the same
content-sniff patterns, invokes checkov, normalises its JSON output (single
object or array), and converts failed_checks to Finding instances.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import site
import subprocess
import tempfile
from pathlib import Path

from ai_pr_review.analyzers._env import tool_env
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

# Content of the trusted checkov config (#739, #1102). Checkov rejects a
# config file that is not a YAML mapping, so a comment-only file made every
# run exit 2 with no scan. `quiet: true` is a valid mapping and repeats a
# flag that the command line already passes. It must not name any option that
# loads code or data from the workspace.
_TRUSTED_CONFIG = "# Trusted config for ai-pr-review; see #739 and #1102.\nquiet: true\n"

# Checkov gets the shared scrubbed environment (`tool_env`). It parses untrusted
# files, so it must not see the job credentials, and it reads many option
# variables (CKV_*, CHECKOV_*, BC_*, PRISMA_*, EXTERNAL_MODULES_DIR and others)
# that must not change what it scans. `tool_env` passes only the search path,
# the locale and the proxy and CA settings, and a variable named in
# AI_ANALYZER_ENV_PASSTHROUGH on a run that is not a fork run.


def _checkov_env(home: str) -> dict[str, str]:
    env = tool_env()
    # A pip user install keeps checkov importable only through the real
    # HOME. Pass the user base so it still works with the empty HOME.
    with contextlib.suppress(Exception):  # a missing user base must not stop the run
        env["PYTHONUSERBASE"] = site.getuserbase()
    env["HOME"] = home
    return env


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


def _repo_relative_path(item: dict[str, object], workspace: str) -> str:
    """Return the finding path relative to the workspace.

    The run starts outside the workspace (#1102), so checkov's own
    `repo_file_path` no longer holds a workspace-relative path. Use the
    absolute path when it lies inside the workspace, and return "" (the caller
    drops the result) when it lies outside. Use the old field only when the
    result carries no absolute path.
    """
    abs_path = item.get("file_abs_path")
    if isinstance(abs_path, str) and abs_path:
        rel = os.path.relpath(abs_path, workspace)
        if rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            return rel
        return ""
    return str(item.get("repo_file_path") or "").lstrip("/")


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

    workspace = os.getcwd()
    file_args: list[str] = []
    for f in target_files:
        file_args += ["--file", os.path.abspath(f)]

    # Never let checkov read .checkov.yaml/.checkov.yml or a baseline from the
    # analyzed workspace: the workspace may be untrusted fork-PR content
    # checked out under pull_request_target (#739), and that config can set
    # `external-checks-dir` to import arbitrary Python check modules from the
    # repo. An explicit --config-file does NOT stop this. Checkov 3.3.30
    # merges the explicit file with the default files it finds in the current
    # directory and in the home directory (checked in #1102). So the run
    # starts in an empty scratch directory, with HOME set to the same empty
    # directory, and the scanned files go in by absolute path. The trusted
    # config only keeps the config-file option well formed.
    try:
        scratch_cm = tempfile.TemporaryDirectory(prefix="checkov-trusted-", ignore_cleanup_errors=True)
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: could not create trusted checkov config: %s; skipping.", exc)
        return []

    with scratch_cm as scratch:
        trusted_config_path = os.path.join(scratch, "trusted-config.yaml")
        try:
            Path(trusted_config_path).write_text(_TRUSTED_CONFIG)
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: could not create trusted checkov config: %s; skipping.", exc)
            return []
        run_dir = os.path.join(scratch, "cwd")
        try:
            os.mkdir(run_dir)
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: could not create checkov work directory: %s; skipping.", exc)
            return []
        env = _checkov_env(run_dir)
        try:
            result = subprocess.run(
                ["checkov", *file_args, "--config-file", trusted_config_path,
                 "--output", "json", "--quiet", "--compact"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECS,
                cwd=run_dir,
                env=env,
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
        if result.returncode != 0:
            logger.warning(
                "[ai-pr-review] WARNING: checkov exited %d with no output; possible installation error. stderr: %s",
                result.returncode, result.stderr[:200],
            )
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
            repo_file = _repo_relative_path(item, workspace)
            if not repo_file:
                logger.warning(
                    "[ai-pr-review] WARNING: checkov result %s has no path inside the workspace; dropped.",
                    check_id,
                )
                continue
            line_range = item.get("file_line_range") or []
            line = line_range[0] if line_range else 1
            guideline = item.get("guideline") or ""
            remediation = guideline if guideline else _DEFAULT_REMEDIATION
            severity = "High" if _HIGH_PREFIX_RE.match(check_id) else "Medium"
            if check_id.startswith("CKV_SECRET_"):
                category = "secret"
            elif check_id.startswith("CKV2_"):
                # CKV2_* are checkov's cross-resource "graph checks" (e.g. an
                # exposed security group, a publicly-readable storage bucket)
                # -- access/exposure policy violations, not style lint.
                category = "authz"
            else:
                # The bulk of single-resource CKV_* checks (missing encryption,
                # public buckets, open security groups, etc.) don't fit "lint" --
                # that's a real category under category-aware clustering and
                # would wall these off from corroborating with an agent's
                # authz/secret finding on the same line. Default to the "other"
                # wildcard instead, matching semgrep's own unclassified default.
                category = "other"

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
