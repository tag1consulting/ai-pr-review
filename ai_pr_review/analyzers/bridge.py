"""Analyzer bridge — invokes run-*.sh wrappers as subprocesses.

Uses the typed ChangedFiles from manifest.py to skip analyzers with no
eligible files (closes #188). Normalizes JSON output into Finding instances.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from pydantic import ValidationError

from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles


class AnalyzerSpec(NamedTuple):
    name: str
    script: str
    # Files that must be non-empty in ChangedFiles for this analyzer to run.
    # Empty list = always run.
    required_file_types: list[str]


_ANALYZERS: list[AnalyzerSpec] = [
    AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"]),
    AnalyzerSpec("trufflehog", "run-trufflehog.sh", []),
    AnalyzerSpec("semgrep", "run-semgrep.sh", []),
    AnalyzerSpec("ruff", "run-ruff.sh", ["python"]),
    AnalyzerSpec("golangci-lint", "run-golangci-lint.sh", ["go"]),
    AnalyzerSpec("hadolint", "run-hadolint.sh", ["dockerfile"]),
    AnalyzerSpec("checkov", "run-checkov.sh", ["terraform", "iac", "dockerfile"]),
    AnalyzerSpec("phpcs", "run-phpcs.sh", ["php"]),
    AnalyzerSpec("phpstan", "run-phpstan.sh", ["php"]),
    AnalyzerSpec("eslint", "run-eslint.sh", ["js_ts"]),
    AnalyzerSpec("kube-linter", "run-kube-linter.sh", ["iac"]),
    AnalyzerSpec("tflint", "run-tflint.sh", ["terraform"]),
    AnalyzerSpec("cve-check", "run-cve-check.sh", ["manifest_lockfile"]),
]

_SUBPROCESS_TIMEOUT_SECS = 120


def run_analyzers(
    changed_files: ChangedFiles,
    diff_file: str,
    script_dir: str,
    *,
    env: dict[str, str] | None = None,
) -> list[Finding]:
    """Run all eligible analyzers and return normalised Finding instances."""
    results: list[Finding] = []
    analyzers_dir = Path(script_dir) / "analyzers"

    for spec in _ANALYZERS:
        if not _is_eligible(spec, changed_files):
            continue

        script_path = analyzers_dir / spec.script
        if not script_path.is_file():
            continue

        findings = _run_analyzer(spec, str(script_path), diff_file, env or {})
        results.extend(findings)

    return results


def _is_eligible(spec: AnalyzerSpec, cf: ChangedFiles) -> bool:
    """Return True if the analyzer has relevant files to inspect."""
    if not spec.required_file_types:
        return True
    return any(bool(getattr(cf, ft, [])) for ft in spec.required_file_types)


def _run_analyzer(
    spec: AnalyzerSpec,
    script_path: str,
    diff_file: str,
    extra_env: dict[str, str],
) -> list[Finding]:
    run_env = {**os.environ, **extra_env, "DIFF_FILE": diff_file}
    try:
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True,
            text=True,
            env=run_env,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"\n[ai-pr-review] WARNING: {spec.name} timed out after {_SUBPROCESS_TIMEOUT_SECS}s; skipping.",
            file=sys.stderr,
        )
        return []
    except OSError as exc:
        print(f"\n[ai-pr-review] WARNING: {spec.name} failed to start: {exc}; skipping.", file=sys.stderr)
        return []

    if result.returncode not in (0, 1):
        print(
            f"\n[ai-pr-review] WARNING: {spec.name} exited {result.returncode}; skipping. "
            f"stderr: {result.stderr[:200]}",
            file=sys.stderr,
        )
        return []

    output = result.stdout.strip()
    if not output:
        return []

    return _normalise_output(output, spec.name)


def _normalise_output(output: str, source: str) -> list[Finding]:
    """Parse JSON array of findings from analyzer stdout."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        print(
            f"\n[ai-pr-review] WARNING: {source} produced non-JSON output; skipping. Preview: {output[:200]}",
            file=sys.stderr,
        )
        return []

    if not isinstance(data, list):
        return []

    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not item.get("source"):
            item["source"] = source
        try:
            findings.append(Finding.model_validate(item))
        except ValidationError as exc:
            print(f"WARNING: {source} dropped malformed finding: {exc}", file=sys.stderr)

    return findings
