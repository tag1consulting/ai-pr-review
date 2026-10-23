"""Fork review policy constants and the trusted-association parser.

A neutral module with no imports from ``ai_pr_review.review`` or ``ai_pr_review.vcs``, so
``config.py`` can read the defaults without importing the gate. ``review/fork_gate.py`` re-exports
every name here for compatibility.
"""

from __future__ import annotations

DEFAULT_FORK_MAX_COST_USD = 2.0
FORK_POLICIES = ("auto", "label", "block")
# The default is block until the live fork end-to-end test passes. Set auto or label to review forks.
DEFAULT_FORK_POLICY = "block"
# An unknown AI_FORK_POLICY value must never open the gate, so it falls back to the safest policy,
# which is NOT the default. Only an unset or empty value gets DEFAULT_FORK_POLICY.
SAFEST_FORK_POLICY = "label"
DEFAULT_TRUSTED_ASSOCIATIONS = ("OWNER", "COLLABORATOR")


def parse_trusted_associations(raw: str | None) -> tuple[str, ...]:
    """Parse ``AI_FORK_TRUSTED_ASSOCIATIONS``. ``None`` (unset) gives the default. An empty value
    trusts nobody."""
    if raw is None:
        return DEFAULT_TRUSTED_ASSOCIATIONS
    return tuple(dict.fromkeys(p.strip().upper() for p in raw.split(",") if p.strip()))
