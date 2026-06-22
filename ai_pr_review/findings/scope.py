"""Diff-scope filtering for native-analyzer findings.

Native static analyzers (phpcs, phpstan, semgrep, SARIF tools) lint the
entire file a PR touches, so a single changed line in a 2,000-line legacy
file can yield hundreds of diagnostics on unchanged code.  This module
provides two orthogonal noise-reduction passes that run after suppression:

1. apply_diff_scope  -- cap out-of-diff analyzer findings to Low severity
   and mark them with out_of_diff=True so the body renderer can collapse
   them into a <details> section.

2. rollup_repeated_findings  -- collapse findings where the same source +
   normalised text fires more than ROLLUP_THRESHOLD times inside a single
   file into one representative entry with an occurrence count and a line
   list appended to the finding text.  Addresses the
   DisallowLongArraySyntax-x55 pattern observed in BRMCloud/opus#1941.

Both passes are controlled by the AI_ANALYZER_DIFF_SCOPE env var (default
"cap").  Passing "off" disables both.  Passing "drop" drops out-of-diff
analyzer findings entirely instead of downgrading them.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ai_pr_review.diff.linemap import parse_added_lines
from ai_pr_review.findings.models import Finding

# Analyzer source prefixes that identify native-tool findings.  LLM-agent
# findings use agent names (code-reviewer, security-reviewer, etc.) which
# do not appear in this set.  These must match the source strings emitted
# by each analyzers/run-*.sh script exactly (using prefix matching).
_ANALYZER_PREFIXES: tuple[str, ...] = (
    "checkov",
    "eslint",
    "golangci-lint",
    "hadolint",
    "kube-linter",
    "osv",
    "phpcs",
    "phpstan",
    "ruff",
    "sarif:",
    "semgrep",
    "shellcheck",
    "tflint",
    "trufflehog",
)

# Compiled whitespace normaliser used by rollup_repeated_findings.
_WHITESPACE = re.compile(r"\s+")

# When the same source+text fires more than this many times in one file,
# collapse the group into a single rollup finding.
ROLLUP_THRESHOLD = 5


def is_analyzer_source(source: str) -> bool:
    """Return True when a single source string is a native static-analyzer tag.

    Matches by prefix against ``_ANALYZER_PREFIXES`` (case-insensitive).  This
    is the canonical per-string predicate reused by ``_is_analyzer`` and by the
    provenance-weighting module so that ``_ANALYZER_PREFIXES`` remains the sole
    source of truth.
    """
    s = source.lower()
    return any(s.startswith(p) for p in _ANALYZER_PREFIXES)


def _is_analyzer(finding: Finding) -> bool:
    """Return True when a finding originates from a native static analyzer."""
    sources = finding.sources or ([finding.source] if finding.source else [])
    return any(is_analyzer_source(s) for s in sources)


def apply_diff_scope(
    findings: list[Finding],
    diff_text: str,
    *,
    mode: str = "cap",
) -> list[Finding]:
    """Apply diff-scope rules to analyzer findings.

    mode="cap"  (default) -- out-of-diff analyzer findings are downgraded
                             to Low and marked out_of_diff=True.
    mode="drop"           -- out-of-diff analyzer findings are removed.
    mode="off"            -- pass through unchanged.

    LLM-agent findings are never touched regardless of mode.
    """
    if mode == "off" or not (diff_text or "").strip():
        return list(findings)

    eligible = {(lr.file, lr.line) for lr in parse_added_lines(diff_text)}

    result: list[Finding] = []
    for f in findings:
        # Treat line=None and line=0 as "no specific line" — some analyzers emit
        # line=0 as a file-level sentinel.  Such findings should pass through
        # unchanged rather than being compared against the in-diff line set.
        if not _is_analyzer(f) or not f.file or not f.line:
            result.append(f)
            continue

        in_diff = (f.file, f.line) in eligible
        if in_diff:
            result.append(f)
        elif mode == "drop":
            pass
        else:
            result.append(f.model_copy(update={"severity": "Low", "out_of_diff": True}))

    return result


def rollup_repeated_findings(
    findings: list[Finding],
    *,
    threshold: int = ROLLUP_THRESHOLD,
) -> list[Finding]:
    """Collapse repeated same-source+text analyzer findings within one file.

    When the same analyzer rule fires more than `threshold` times in a single
    file, all occurrences are collapsed into one representative Finding.  The
    finding text gains a suffix like "(42 occurrences, lines: 15, 32, 47 ...)"
    so reviewers can see the scope without reading 42 separate entries.

    Non-analyzer findings and findings without a file are left unchanged.
    """
    def _normalise(text: str) -> str:
        return _WHITESPACE.sub(" ", text.strip().lower())

    # Group analyzer findings by (file, source, normalised_text, severity, out_of_diff).
    # Both severity and out_of_diff are part of the key so:
    # - In-diff and out-of-diff occurrences of the same rule are never collapsed
    #   together (a genuine in-diff High could otherwise be demoted to Low).
    # - Findings of the same rule but different severities are kept separate
    #   (important when mode='off' leaves all out_of_diff flags as False).
    #
    # Output ordering: passthrough (non-analyzer / no-file) findings come
    # first, followed by analyzer groups in the order their first member was
    # encountered.  Callers must not rely on relative ordering between
    # passthrough and grouped findings.
    groups: dict[tuple[str, str, str, str, bool], list[Finding]] = defaultdict(list)
    passthrough: list[Finding] = []

    for f in findings:
        if _is_analyzer(f) and f.file:
            src = f.source or (f.sources[0] if f.sources else "")
            key = (f.file, src, _normalise(f.finding), f.severity, f.out_of_diff)
            groups[key].append(f)
        else:
            passthrough.append(f)

    result: list[Finding] = list(passthrough)
    for group_key, group in groups.items():
        file = group_key[0]
        if len(group) <= threshold:
            result.extend(group)
            continue

        # Collapse: keep the lowest-line representative, append occurrence summary.
        rep = min(group, key=lambda f: (f.line or 0))
        lines = sorted({f.line for f in group if f.line is not None})
        line_preview = ", ".join(str(n) for n in lines[:10])
        if len(lines) > 10:
            line_preview += f" ... (+{len(lines) - 10} more)"
        summary = f" ({len(group)} occurrences in {file}, lines: {line_preview})"
        result.append(rep.model_copy(update={"finding": rep.finding + summary}))

    return result
