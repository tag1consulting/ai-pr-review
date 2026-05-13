"""Per-agent SHA watermark advance policy — resolves #182."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")

_HELD_MESSAGE = (
    "Watermark held at previous SHA — the following agents did not complete: {agents}. "
    "Re-run or next push will re-review from the previous watermark."
)


@dataclass(frozen=True)
class WatermarkPolicy:
    advance_global: bool
    new_global_sha: str | None
    per_agent: Mapping[str, str]
    body_explanation: str


def _is_valid_sha(sha: str) -> bool:
    return bool(_SHA_PATTERN.match(sha))


def decide_watermark_advance(
    head_sha: str,
    succeeded_agents: Sequence[str],
    failed_agents: Sequence[str],
    required_for_global: Sequence[str] | None = None,
) -> WatermarkPolicy:
    """Return a policy describing whether/how to advance watermarks.

    Policy rules:
    - Invalid head_sha → no advance at all.
    - An agent appearing in both succeeded and failed lists is treated as failed.
    - per_agent contains only genuinely-succeeded agents.
    - required_for_global=None (default) means "every agent that ran is required"
      — any failure blocks the global advance.
    - required_for_global=[] (explicit empty) means "none required" — global
      advances as long as head_sha is valid.
    - Otherwise: the listed agents must all be in the effective-succeeded set
      (not in failed_agents) for global to advance.
    """
    if not _is_valid_sha(head_sha):
        return WatermarkPolicy(
            advance_global=False,
            new_global_sha=None,
            per_agent={},
            body_explanation=f"Invalid head SHA {head_sha!r}; watermark not advanced.",
        )

    failed_set = set(failed_agents)
    # Conservative ambiguity: if an agent is in both lists, drop it from succeeded.
    effective_succeeded = [a for a in succeeded_agents if a not in failed_set]
    per_agent: dict[str, str] = {a: head_sha for a in effective_succeeded}

    if required_for_global is None:
        blocking_failures = sorted(failed_set)
    else:
        required_set = set(required_for_global)
        blocking_failures = sorted(required_set & failed_set)

    if blocking_failures:
        return WatermarkPolicy(
            advance_global=False,
            new_global_sha=None,
            per_agent=per_agent,
            body_explanation=_HELD_MESSAGE.format(agents=", ".join(blocking_failures)),
        )

    return WatermarkPolicy(
        advance_global=True,
        new_global_sha=head_sha,
        per_agent=per_agent,
        body_explanation="",
    )
