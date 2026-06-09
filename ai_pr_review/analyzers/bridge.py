"""Analyzer bridge — invokes static analyzers and returns Finding instances.

Dispatches to native Python callables (Epic 8) when available, falling back
to run-*.sh wrappers via subprocess for tools not yet ported.

Uses the typed ChangedFiles from manifest.py to skip analyzers with no
eligible files (closes #188). Normalizes JSON output into Finding instances.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import anyio
from pydantic import ValidationError

# Native analyzer imports (Epic 8). Each ported analyzer is imported here and
# wired into _ANALYZERS via the native_fn field.
from ai_pr_review.analyzers.native.hadolint import _run_hadolint
from ai_pr_review.analyzers.native.kube_linter import _run_kube_linter
from ai_pr_review.analyzers.native.phpcs import _run_phpcs
from ai_pr_review.analyzers.native.ruff import _run_ruff
from ai_pr_review.analyzers.native.shellcheck import _run_shellcheck
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

NativeAnalyzerFn = Callable[[ChangedFiles, Path], list[Finding]]


class AnalyzerSpec(NamedTuple):
    name: str
    script: str
    # Files that must be non-empty in ChangedFiles for this analyzer to run.
    # Empty list = always run.
    required_file_types: list[str]
    # When set, the native Python callable is used instead of the bash script.
    native_fn: NativeAnalyzerFn | None = None


_ANALYZERS: list[AnalyzerSpec] = [
    AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"], _run_shellcheck),
    AnalyzerSpec("trufflehog", "run-trufflehog.sh", []),
    AnalyzerSpec("semgrep", "run-semgrep.sh", []),
    AnalyzerSpec("ruff", "run-ruff.sh", ["python"], _run_ruff),
    AnalyzerSpec("golangci-lint", "run-golangci-lint.sh", ["go"]),
    AnalyzerSpec("hadolint", "run-hadolint.sh", ["dockerfile"], _run_hadolint),
    AnalyzerSpec("checkov", "run-checkov.sh", ["terraform", "iac", "dockerfile"]),
    AnalyzerSpec("phpcs", "run-phpcs.sh", ["php"], _run_phpcs),
    AnalyzerSpec("phpstan", "run-phpstan.sh", ["php"]),
    AnalyzerSpec("eslint", "run-eslint.sh", ["js_ts"]),
    AnalyzerSpec("kube-linter", "run-kube-linter.sh", ["iac"], _run_kube_linter),
    AnalyzerSpec("tflint", "run-tflint.sh", ["terraform"]),
    AnalyzerSpec("cve-check", "run-cve-check.sh", ["manifest_lockfile"]),
]

_SUBPROCESS_TIMEOUT_SECS = 120

# #353: Native analyzer names that can be skipped when equivalent SARIF is supplied.
# Keyed by analyzer name (as in AnalyzerSpec.name). The caller derives which names
# are covered by inspecting the sarif_paths configuration and passes them here.
_SARIF_EQUIVALENT_ANALYZERS: frozenset[str] = frozenset({"ruff", "semgrep", "hadolint"})


def _sarif_covered_names(sarif_paths: tuple[str, ...]) -> frozenset[str]:
    """Return analyzer names whose SARIF equivalent is in sarif_paths.

    Matching is done on the filename stem (case-normalized), so "ruff.sarif" covers
    the "ruff" native analyzer. No file I/O or JSON parsing is performed; a malformed
    or missing path simply produces no match, which means the native wrapper runs as
    normal (fail-soft).
    """
    covered: set[str] = set()
    for path in sarif_paths:
        stem = Path(path).stem.lower()
        if stem in _SARIF_EQUIVALENT_ANALYZERS:
            covered.add(stem)
    return frozenset(covered)


def _file_list(cf: ChangedFiles) -> str:
    """Return a sorted, deduplicated newline-joined string of all changed file paths."""
    return "\n".join(sorted(set(cf.all_files)))


async def run_analyzers(
    changed_files: ChangedFiles,
    diff_file: str,
    script_dir: str,
    *,
    env: dict[str, str] | None = None,
    concurrency: int = 4,
    sarif_skip: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Run eligible analyzers concurrently and return normalised Finding instances.

    Args:
        changed_files: Files changed in the PR.
        diff_file: Path to the diff file consumed by analyzer wrappers.
        script_dir: Directory containing the analyzers/ subdirectory.
        env: Extra environment variables forwarded to each wrapper.
        concurrency: Maximum simultaneous analyzer subprocesses.
        sarif_skip: Analyzer names to skip because equivalent SARIF is supplied.
            Derive with _sarif_covered_names(config.sarif_paths).
    """
    analyzers_dir = Path(script_dir) / "analyzers"
    file_list = _file_list(changed_files)

    # Pre-select eligible specs. Native analyzers don't require a script file on disk.
    eligible = [
        spec for spec in _ANALYZERS
        if _is_eligible(spec, changed_files)
        and (spec.native_fn is not None or (analyzers_dir / spec.script).is_file())
        and spec.name not in sarif_skip
    ]

    # Log any skipped-due-to-SARIF entries so operators can verify the substitution.
    sarif_skipped = [
        spec for spec in _ANALYZERS
        if _is_eligible(spec, changed_files)
        and (spec.native_fn is not None or (analyzers_dir / spec.script).is_file())
        and spec.name in sarif_skip
    ]
    for spec in sarif_skipped:
        print(
            f"[ai-pr-review] INFO: skipping native {spec.name!r} analyzer "
            "(equivalent SARIF file configured via AI_SARIF_PATHS).",
            file=sys.stderr,
        )

    # Pre-allocate a slot per eligible spec so results come back in spec order
    # regardless of which task finishes first.
    slots: list[list[Finding]] = [[] for _ in eligible]
    limiter = anyio.CapacityLimiter(max(1, concurrency))

    async def _run_slot(idx: int, spec: AnalyzerSpec) -> None:
        if spec.native_fn is not None:
            try:
                findings = await anyio.to_thread.run_sync(
                    functools.partial(spec.native_fn, changed_files, Path(diff_file)),
                    cancellable=True,
                    limiter=limiter,
                )
            except BaseException as exc:
                if isinstance(exc, (anyio.get_cancelled_exc_class(), KeyboardInterrupt, SystemExit)):
                    raise
                print(
                    f"\n[ai-pr-review] WARNING: {spec.name} native analyzer raised an unexpected error: "
                    f"{type(exc).__name__}: {exc}; skipping.\n"
                    f"{traceback.format_exc()}",
                    file=sys.stderr,
                )
                findings = []
            slots[idx] = findings
            return

        script_path = str(analyzers_dir / spec.script)
        try:
            findings = await anyio.to_thread.run_sync(
                functools.partial(
                    _run_analyzer, spec, script_path, diff_file, env or {}, file_list
                ),
                cancellable=True,
                limiter=limiter,
            )
        except BaseException as exc:
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            print(
                f"\n[ai-pr-review] WARNING: {spec.name} raised an unexpected error: "
                f"{type(exc).__name__}: {exc}; skipping.\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
            findings = []
        slots[idx] = findings

    async with anyio.create_task_group() as tg:
        for idx, spec in enumerate(eligible):
            tg.start_soon(_run_slot, idx, spec)

    # Flatten in spec order for deterministic output.
    results: list[Finding] = []
    for slot in slots:
        results.extend(slot)
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
    file_list: str = "",
) -> list[Finding]:
    run_env = {**os.environ, **extra_env, "DIFF_FILE": diff_file}
    try:
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True,
            text=True,
            input=file_list,
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
