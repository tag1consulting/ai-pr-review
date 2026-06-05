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
