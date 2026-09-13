"""Shared subprocess/JSON scaffold for the simple CLI-wrapping analyzers.

Factors out the boilerplate duplicated, near-verbatim, across the analyzer
modules whose native.<tool>.py file does no more than: shell out to a CLI
tool, parse its JSON stdout, and map each reported item to a Finding via a
per-tool severity rule (eslint, shellcheck, hadolint, ruff, kube-linter).

Deliberately NOT used by every analyzer. golangci-lint (module-root
discovery + tempfile JSON output path), tflint/phpstan/checkov (a
security-sensitive trusted-empty-config tempfile per #739, and for tflint/
phpstan also per-directory or level-dependent dispatch), phpcs (standard
autodetection + a generated ruleset), cve-check (no subprocess at all --
lockfile parsing + an OSV.dev HTTP batch call), trufflehog (NDJSON output +
an allowlist file + test-path classification), semgrep (a whole
config-resolution + check-id category-hint layer), and the docs/* analyzers
(tree-sitter, not a CLI tool) all have real per-tool setup or output shapes
that don't fit this one shared shape without turning it into a pile of
special-cased flags. Forcing them through here would be a lossy abstraction,
not a simplification -- see issue #823.

Each analyzer using this scaffold still owns: its own `shutil.which(...)`
check (message wording differs slightly per tool today, e.g. eslint's "tried
node_modules/.bin/eslint and npx"), building the `command` argv, and the
`extract_items`/`build_finding` callbacks (JSON shapes and per-item field
names differ enough per tool that flattening them into one generic accessor
would just move the special-casing here instead of removing it). What this
scaffold owns is the part that was byte-for-byte identical across all five:
running the subprocess with timeout/OSError handling, checking the
returncode, decoding JSON with a uniform non-JSON warning, and wrapping each
Finding construction in the same (ValueError, TypeError)-tolerant guard.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from ai_pr_review.findings.models import Finding

logger = logging.getLogger(__name__)

_DEFAULT_SUCCESS_RETURNCODES = frozenset({0, 1})


def run_cli_json_analyzer(
    *,
    tool: str,
    command: Sequence[str],
    extract_items: Callable[[Any], list[Any] | None],
    build_finding: Callable[[Any], Finding | None],
    success_returncodes: frozenset[int] = _DEFAULT_SUCCESS_RETURNCODES,
    timeout_secs: int = 120,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    on_bad_returncode: Callable[[subprocess.CompletedProcess[str]], None] | None = None,
) -> list[Finding]:
    """Run *command* (a tool named *tool*), parse its JSON stdout, and convert
    each item *extract_items* pulls out of the parsed document to a Finding
    via *build_finding*.

    Callers must have already confirmed the binary exists (`shutil.which`)
    before calling this -- that check stays in each analyzer module since the
    not-found message wording is tool-specific.

    `extract_items(data)` receives the `json.loads()`-parsed document and
    returns the list of raw per-finding items to iterate, or `None` if the
    document's shape is not what was expected (it is responsible for logging
    its own warning in that case, since the expected shape and its warning
    message are tool-specific).

    `build_finding(item)` receives one raw item and returns a `Finding`, or
    `None` to skip it silently. Raising `ValueError`/`TypeError` is treated as
    a malformed item: logged once with a uniform message and skipped, rather
    than aborting the whole run.

    `on_bad_returncode`, if given, replaces the generic "tool exited N;
    skipping" warning for a returncode outside `success_returncodes` --
    eslint's exit code 2 (a fatal config/plugin error, worth a more specific
    message) is the one case among this scaffold's callers that needs this.
    """
    try:
        result = subprocess.run(  # noqa: S603
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "[ai-pr-review] WARNING: %s timed out after %ss; skipping.", tool, exc.timeout
        )
        return []
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: %s failed to start: %s", tool, exc)
        return []

    if result.returncode not in success_returncodes:
        if on_bad_returncode is not None:
            on_bad_returncode(result)
        else:
            logger.warning(
                "[ai-pr-review] WARNING: %s exited %d; skipping. stderr: %s",
                tool, result.returncode, result.stderr[:200],
            )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: %s produced non-JSON output: %s", tool, exc)
        return []

    items = extract_items(data)
    if not items:
        return []

    findings: list[Finding] = []
    for item in items:
        try:
            finding = build_finding(item)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[ai-pr-review] WARNING: %s dropped malformed finding: %s; item=%r",
                tool, exc, repr(item)[:200],
            )
            continue
        if finding is not None:
            findings.append(finding)

    return findings
