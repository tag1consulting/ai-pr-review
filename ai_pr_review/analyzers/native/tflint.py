"""Native Python implementation of the tflint analyzer.

Replaces analyzers/run-tflint.sh. Runs tflint per unique Terraform directory
using --chdir, prepends the directory prefix to bare filenames, and converts
findings to Finding instances.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "tflint"
_TIMEOUT_SECS = 120
_DEFAULT_REMEDIATION = "See https://github.com/terraform-linters/tflint-ruleset-aws"


def _run_tflint(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run tflint on changed Terraform files and return Finding instances."""
    target_files = [f for f in changed_files.terraform if Path(f).is_file()]
    if not target_files:
        return []

    if not shutil.which("tflint"):
        logger.warning("[ai-pr-review] WARNING: tflint not found; skipping.")
        return []

    # Collect unique directories; tflint runs per-module-directory.
    dirs: list[str] = []
    seen_dirs: set[str] = set()
    for f in target_files:
        d = str(Path(f).parent)
        if d not in seen_dirs:
            seen_dirs.add(d)
            dirs.append(d)

    # Never let tflint auto-discover .tflint.hcl/.tflint.json or resolve
    # plugins from ./.tflint.d/plugins in the analyzed workspace: the
    # workspace may be untrusted fork-PR content checked out under
    # pull_request_target (#739), and both a config's `plugin_dir` directive
    # and the default ./.tflint.d/plugins lookup let a fork point tflint at
    # a plugin binary it committed, which tflint executes as a subprocess.
    # --config always takes priority over auto-discovery of .tflint.hcl, and
    # TFLINT_PLUGIN_DIR is consulted before the ./.tflint.d/plugins fallback
    # (but after a config-set plugin_dir, hence the trusted config below
    # deliberately sets none) -- together they guarantee no workspace
    # config or plugin is ever honored, regardless of trust context.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".hcl", prefix="tflint-trusted-", delete=False
        ) as trusted_config:
            trusted_config.write("# Deliberately empty; see #739.\n")
            trusted_config_path = trusted_config.name
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: could not create trusted tflint config: %s; skipping.", exc)
        return []

    env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory(prefix="tflint-plugins-") as empty_plugin_dir:
            env["TFLINT_PLUGIN_DIR"] = empty_plugin_dir

            all_findings: list[Finding] = []
            for d in dirs:
                try:
                    result = subprocess.run(
                        ["tflint", f"--chdir={d}", f"--config={trusted_config_path}", "--format=json"],
                        capture_output=True,
                        text=True,
                        timeout=_TIMEOUT_SECS,
                        env=env,
                    )
                except subprocess.TimeoutExpired as exc:
                    logger.warning("[ai-pr-review] WARNING: tflint timed out after %ss in %r; skipping.", exc.timeout, d)
                    continue
                except OSError as exc:
                    logger.warning("[ai-pr-review] WARNING: tflint failed to start in %r: %s", d, exc)
                    continue

                if result.returncode >= 2:
                    logger.warning(
                        "[ai-pr-review] WARNING: tflint exited %d in %r; stderr: %s",
                        result.returncode, d, result.stderr[:200],
                    )
                    continue

                if not result.stdout.strip():
                    continue

                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    logger.warning("[ai-pr-review] WARNING: tflint produced non-JSON output in %r: %s", d, exc)
                    continue

                if not isinstance(data, dict):
                    logger.warning(
                        "[ai-pr-review] WARNING: tflint produced unexpected output structure in %r; skipping.", d
                    )
                    continue

                issues = data.get("issues") or []
                if not isinstance(issues, list):
                    logger.warning("[ai-pr-review] WARNING: tflint 'issues' is not a list in %r; skipping.", d)
                    continue

                for err in data.get("errors") or []:
                    if isinstance(err, dict):
                        logger.warning(
                            "[ai-pr-review] WARNING: tflint reported error in %r: %s",
                            d, err.get("message", repr(err))[:200],
                        )

                for item in issues:
                    if not isinstance(item, dict):
                        continue
                    rule = item.get("rule") or {}
                    rule_name = rule.get("name") or ""
                    rule_severity = rule.get("severity") or ""
                    rule_link = rule.get("link") or ""
                    message = item.get("message") or ""
                    range_info = item.get("range") or {}
                    filename = range_info.get("filename") or ""
                    start = range_info.get("start") or {}
                    line = start.get("line") or 1

                    if rule_severity == "error":
                        severity = "High"
                    elif rule_severity == "warning":
                        severity = "Medium"
                    else:
                        severity = "Low"

                    remediation = f"See {rule_link}" if rule_link else _DEFAULT_REMEDIATION

                    try:
                        all_findings.append(
                            Finding(
                                severity=severity,  # type: ignore[arg-type]
                                confidence=_CONFIDENCE,
                                source=_SOURCE,
                                file=str(Path(d) / filename) if filename else filename,
                                line=line,
                                finding=f"{rule_name}: {message}",
                                remediation=remediation,
                                category="lint",
                            )
                        )
                    except (ValueError, TypeError) as exc:
                        logger.warning(
                            "[ai-pr-review] WARNING: tflint dropped malformed finding: %s; item=%r",
                            exc, repr(item)[:200],
                        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(trusted_config_path)

    return all_findings
