"""Extract findings from agent output text.

Ports lib/findings.sh extract_findings: parses json-findings fenced
blocks, stamps source, validates shape.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass

from ai_pr_review.findings.models import INTERNAL_ONLY_FIELDS, Finding

_FENCE_RE = re.compile(r"```json-findings[ \t]*\r?\n(.*?)```", re.DOTALL)

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


@dataclass(frozen=True)
class Extraction:
    """What one agent output gave, from a single parse (#1088).

    An empty ``findings`` list can mean the agent found nothing, the answer
    could not be read, or the findings were dropped. ``status`` and ``emitted``
    tell these apart. Both come from the same parse that builds ``findings``,
    so they cannot disagree with it.

    ``status``:

    * ``ok``: a ``json-findings`` block holds a JSON array (it may be empty).
    * ``no_block``: no ``json-findings`` block.
    * ``truncated_no_block``: no block, and the output stopped at the token cap.
    * ``invalid_json``: the block is not valid JSON.
    * ``truncated_invalid``: the output stopped at the token cap, and the block
      is not valid JSON and could not be repaired.
    * ``truncated_repaired``: the output stopped at the token cap, and the
      complete findings before the cut were recovered.
    * ``not_array``: the block is JSON, but not an array.

    ``emitted`` is the number of items in the JSON array, or ``None`` when no
    array could be read. ``emitted`` above ``len(findings)`` means items were
    dropped inside the parse (not an object, failed validation, or self-refuting).
    """

    findings: list[Finding]
    status: str
    emitted: int | None


def extract_findings(
    agent_output: str,
    agent_name: str = "unknown",
    *,
    truncated: bool = False,
) -> list[Finding]:
    """Parse json-findings block(s) from agent output text.

    Returns a (possibly empty) list of validated Finding instances.
    Logs warnings to stderr on malformed input.
    """
    return extract_with_status(agent_output, agent_name, truncated=truncated).findings


# Statuses where the agent's answer could not be read as a findings list (CAP-5).
# `truncated_repaired` is deliberately not listed: the repair kept at least one
# complete finding, so the agent did give a readable answer, only a short one.
# An `ok` block holding `[]` is an honest empty result and is not listed either.
UNREADABLE_STATUSES = frozenset(
    {"no_block", "truncated_no_block", "invalid_json", "truncated_invalid", "not_array"}
)


def is_unreadable(status: str) -> bool:
    """True when the agent output gave no readable findings list."""
    return status in UNREADABLE_STATUSES


def extract_with_status(
    agent_output: str,
    agent_name: str = "unknown",
    *,
    truncated: bool = False,
) -> Extraction:
    """Like ``extract_findings``, and also say what the output held (#1088)."""
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
        return Extraction([], "truncated_no_block" if truncated else "no_block", None)

    raw = match.group(1).strip()
    return _parse_and_validate(raw, agent_name, truncated=truncated)


def _parse_and_validate(
    raw: str,
    agent_name: str,
    *,
    truncated: bool = False,
) -> Extraction:
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
        return Extraction([], "truncated_invalid" if truncated else "invalid_json", None)

    if not isinstance(data, list):
        print(
            f"WARNING: {agent_name} json-findings is not a JSON array; skipping.",
            file=sys.stderr,
        )
        return Extraction([], "not_array", None)

    results: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Stamp source unconditionally (issue #913): an agent-supplied
        # "source" or "sources" is untrusted LLM output. format_source_tag()
        # renders both raw (no sanitize_display_text), and extract_id_map/
        # extract_acks/extract_judge_map read whichever marker-shaped string
        # appears last in the body -- so a prompt-injected "source" (or
        # "sources", which format_source_tag prefers) containing a forged
        # marker can override the real id-map/acks/judge-map, letting a
        # maintainer's dismiss/wont-fix F<n> command suppress an
        # attacker-chosen finding instead of the one actually shown. The
        # #886 fix hardened this for the *verdicts* marker only; this closes
        # the same channel at its one entry point (this is the only place
        # untrusted LLM-agent JSON becomes a Finding -- analyzer/SARIF
        # findings never pass through here).
        item["source"] = agent_name
        item.pop("sources", None)
        # Drop every field that only the pipeline sets (#913, #1074, #1085). An
        # injected value would fake corroboration or a judge verdict, hide the
        # finding from the CHANGES_REQUESTED check, keep it out of a cluster, or
        # skip dismissal matching. A listed set protects a new field by default,
        # and a test requires every `Finding` field to be classified.
        for name in INTERNAL_ONLY_FIELDS:
            item.pop(name, None)
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
            results.append(f)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: {agent_name} dropped malformed finding: {exc}", file=sys.stderr)

    return Extraction(results, "ok", len(data))


def _try_repair(raw: str, agent_name: str) -> Extraction | None:
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
        findings = _parse_and_validate(json.dumps(data), agent_name).findings
        if findings:
            print(
                f"NOTE: {agent_name} was truncated; salvaged {len(findings)} finding(s) from partial JSON.",
                file=sys.stderr,
            )
            return Extraction(findings, "truncated_repaired", len(data))
    return None
