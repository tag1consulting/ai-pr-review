"""Extract findings from agent output text.

Ports lib/findings.sh extract_findings: parses json-findings fenced
blocks, stamps source, validates shape.
"""

from __future__ import annotations

import json
import re
import sys

from ai_pr_review.findings.models import Finding

_FENCE_RE = re.compile(r"```json-findings\n(.*?)```", re.DOTALL)

# Phrases that indicate the finding's own narrative refutes itself.
# Defense-in-depth backstop for the prompt directive in _governance.md
# rule 1 ("Do Not Emit Self-Refuting Findings"). When a finding's
# `finding` text matches one of these patterns, the agent reasoned
# through the issue and concluded it is not real but emitted the
# finding anyway. Drop it; do not post a refuted finding to the PR.
#
# Patterns are conservative: each must be unambiguous in context.
# False-positive risk is asymmetric — dropping a genuine finding whose
# narrative happens to contain "no bug" in some other clause is worse
# than missing a refutation phrasing the agent invented. When in doubt,
# rely on the prompt directive and leave a phrasing for a future bump.
_REFUTATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bno (?:actual|actionable)?\s*(?:bug|issue|problem)\b", re.IGNORECASE),
    re.compile(r"\bwithdraw(?:n|ing)?\b", re.IGNORECASE),
    re.compile(r"\bon (?:closer|further) (?:inspection|examination|review|reading)[^.]*\bcorrect\b", re.IGNORECASE),
    re.compile(r"\bactually[^.]*\bcorrect\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(?:logic|code|behaviour|behavior)\s+is\s+(?:actually\s+)?correct\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:real|true)\s+(?:risk|concern)\b", re.IGNORECASE),
)


def _is_self_refuting(text: str) -> bool:
    """Return True if ``text`` contains a phrase that refutes the finding.

    Used to drop findings whose own narrative concludes the issue is not
    real (per _governance.md rule 1). See ``_REFUTATION_PATTERNS``.
    """
    return any(p.search(text) for p in _REFUTATION_PATTERNS)


def extract_findings(
    agent_output: str,
    agent_name: str = "unknown",
    *,
    truncated: bool = False,
) -> list[Finding]:
    """Parse json-findings block(s) from agent output text.

    Returns a (possibly empty) list of validated Finding instances.
    Logs warnings to stderr on malformed input (matching bash behaviour).
    """
    match = _FENCE_RE.search(agent_output)
    if not match:
        if truncated:
            print(
                f"WARNING: {agent_name} was truncated before json-findings block; findings lost.",
                file=sys.stderr,
            )
        else:
            print(
                f"WARNING: {agent_name} has no json-findings block; skipping.",
                file=sys.stderr,
            )
        return []

    raw = match.group(1).strip()
    return _parse_and_validate(raw, agent_name, truncated=truncated)


def _parse_and_validate(
    raw: str,
    agent_name: str,
    *,
    truncated: bool = False,
) -> list[Finding]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if truncated:
            salvaged = _try_repair(raw, agent_name)
            if salvaged is not None:
                return salvaged
        print(
            f"WARNING: {agent_name} produced invalid json-findings JSON; skipping. "
            f"Preview: {raw[:200]}",
            file=sys.stderr,
        )
        return []

    if not isinstance(data, list):
        print(
            f"WARNING: {agent_name} json-findings is not a JSON array; skipping.",
            file=sys.stderr,
        )
        return []

    results: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Stamp source if absent
        if not item.get("source"):
            item["source"] = agent_name
        try:
            f = Finding.model_validate(item)
            # Drop self-refuting findings whose own narrative concludes the
            # issue is not real (e.g. "...on closer inspection the logic is
            # correct.", "no bug", "withdraw"). Backstop for _governance.md
            # rule 1; the prompt is the primary control.
            combined = f"{f.finding}\n{f.remediation}"
            if _is_self_refuting(combined):
                preview = f.finding[:120].replace("\n", " ")
                print(
                    f"WARNING: {agent_name} dropped self-refuting finding "
                    f"({f.severity}): {preview}",
                    file=sys.stderr,
                )
                continue
            # Strip out_of_diff: it is set internally by apply_diff_scope, not
            # by agents.  An injected "out_of_diff": true in agent JSON would
            # otherwise suppress the finding from CHANGES_REQUESTED evaluation.
            if f.out_of_diff:
                f = f.model_copy(update={"out_of_diff": False})
            results.append(f)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: {agent_name} dropped malformed finding: {exc}", file=sys.stderr)

    return results


def _try_repair(raw: str, agent_name: str) -> list[Finding] | None:
    """Attempt to salvage truncated JSON by finding the last complete object."""
    lines = raw.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if "}" not in lines[i]:
            continue
        candidate_lines = lines[: i + 1]
        # Remove trailing comma from last object if present
        candidate_lines[-1] = candidate_lines[-1].rstrip(",")
        candidate = "\n".join(candidate_lines) + "\n]"
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        findings = _parse_and_validate(json.dumps(data), agent_name)
        if findings:
            print(
                f"NOTE: {agent_name} was truncated; salvaged {len(findings)} finding(s) from partial JSON.",
                file=sys.stderr,
            )
            return findings
    return None
