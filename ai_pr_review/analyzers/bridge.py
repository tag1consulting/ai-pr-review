"""Analyzer bridge — invokes static analyzers and returns Finding instances.

Dispatches to native Python callables for each of the 13 supported analyzers.
Uses the typed ChangedFiles from manifest.py to skip analyzers with no
eligible files (closes #188). Returns Finding instances directly from each
native callable.
"""

from __future__ import annotations

import functools
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import anyio

# Native analyzer imports (Epic 8). Each analyzer is implemented natively in
# ai_pr_review/analyzers/native/ and wired into _ANALYZERS below.
from ai_pr_review.analyzers.native.checkov import _run_checkov
from ai_pr_review.analyzers.native.cve_check import _run_cve_check
from ai_pr_review.analyzers.native.eslint import _run_eslint
from ai_pr_review.analyzers.native.golangci_lint import _run_golangci_lint
from ai_pr_review.analyzers.native.hadolint import _run_hadolint
from ai_pr_review.analyzers.native.kube_linter import _run_kube_linter
from ai_pr_review.analyzers.native.phpcs import _run_phpcs
from ai_pr_review.analyzers.native.phpstan import _run_phpstan
from ai_pr_review.analyzers.native.ruff import _run_ruff
from ai_pr_review.analyzers.native.semgrep import _run_semgrep
from ai_pr_review.analyzers.native.shellcheck import _run_shellcheck
from ai_pr_review.analyzers.native.tflint import _run_tflint
from ai_pr_review.analyzers.native.trufflehog import _run_trufflehog
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

NativeAnalyzerFn = Callable[[ChangedFiles, Path], list[Finding]]


class AnalyzerSpec(NamedTuple):
    name: str
    # Files that must be non-empty in ChangedFiles for this analyzer to run.
    # Empty list = always run.
    required_file_types: list[str]
    native_fn: NativeAnalyzerFn


_ANALYZERS: list[AnalyzerSpec] = [
    AnalyzerSpec("shellcheck",    ["shell"],                    _run_shellcheck),
    AnalyzerSpec("trufflehog",    [],                           _run_trufflehog),
    AnalyzerSpec("semgrep",       [],                           _run_semgrep),
    AnalyzerSpec("ruff",          ["python"],                   _run_ruff),
    AnalyzerSpec("golangci-lint", ["go"],                       _run_golangci_lint),
    AnalyzerSpec("hadolint",      ["dockerfile"],               _run_hadolint),
    AnalyzerSpec("checkov",       ["terraform", "iac", "dockerfile"], _run_checkov),
    AnalyzerSpec("phpcs",         ["php"],                      _run_phpcs),
    AnalyzerSpec("phpstan",       ["php"],                      _run_phpstan),
    AnalyzerSpec("eslint",        ["js_ts"],                    _run_eslint),
    AnalyzerSpec("kube-linter",   ["iac"],                      _run_kube_linter),
    AnalyzerSpec("tflint",        ["terraform"],                _run_tflint),
    AnalyzerSpec("cve-check",     ["manifest_lockfile"],        _run_cve_check),
]

# Public canonical set — used by config validation for the analyzers allowlist/denylist inputs.
ANALYZER_NAMES: frozenset[str] = frozenset(spec.name for spec in _ANALYZERS)

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


def _analyzer_skip_names(
    allow: tuple[str, ...],
    deny: tuple[str, ...],
) -> frozenset[str]:
    """Collapse allowlist/denylist config into a set of analyzer names to skip.

    Semantics:
    - If *allow* is non-empty, only analyzers in *allow* are permitted; every other
      analyzer name is in the returned skip set. *deny* is ignored (allowlist takes
      precedence).
    - If *allow* is empty, the returned skip set equals *deny*.
    - Both empty (the default) => empty skip set (no-op / zero behavioral change).
    """
    if allow:
        return ANALYZER_NAMES - frozenset(allow)
    return frozenset(deny)


def _file_list(cf: ChangedFiles) -> str:
    """Return a sorted, deduplicated newline-joined string of all changed file paths."""
    return "\n".join(sorted(set(cf.all_files)))


async def run_analyzers(
    changed_files: ChangedFiles,
    diff_file: str,
    *,
    concurrency: int = 4,
    sarif_skip: frozenset[str] = frozenset(),
    disabled: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Run eligible analyzers concurrently and return Finding instances.

    Args:
        changed_files: Files changed in the PR.
        diff_file: Path to the diff file passed to each native analyzer.
        concurrency: Maximum simultaneous analyzer tasks.
        sarif_skip: Analyzer names to skip because equivalent SARIF is supplied.
            Derive with _sarif_covered_names(config.sarif_paths).
        disabled: Analyzer names to skip due to allowlist/denylist configuration.
            Derive with _analyzer_skip_names(config.analyzers, config.exclude_analyzers).
            Kept separate from sarif_skip so the SARIF substitution log message stays
            truthful.
    """
    # Pre-select eligible specs.
    eligible = [
        spec for spec in _ANALYZERS
        if _is_eligible(spec, changed_files)
        and spec.name not in sarif_skip
        and spec.name not in disabled
    ]

    # Log any skipped-due-to-SARIF entries so operators can verify the substitution.
    sarif_skipped = [
        spec for spec in _ANALYZERS
        if _is_eligible(spec, changed_files)
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
