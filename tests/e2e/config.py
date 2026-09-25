"""Static per-platform configuration for the deterministic e2e harness.

Everything here is data, not behavior. Adapters (tests/e2e/platforms.py) and
the verifier (tests/e2e/verify.py) consume these values; nothing in this
module performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Sourced from the actual ReviewConfig.resolve_models() defaults rather than
# a duplicated string literal here -- if config.py's provider defaults ever
# change, this harness picks it up automatically instead of silently
# drifting from what actually ships.
from ai_pr_review.config import ReviewConfig


def _current_default_models(provider: str) -> tuple[str, str]:
    resolved = ReviewConfig(provider=provider).resolve_models()
    return resolved.model_standard, resolved.model_premium

# ---------------------------------------------------------------------------
# Pinned seed commit
# ---------------------------------------------------------------------------

# TODO(before first use): replace with the real, current tip commit SHA of
# the `test/v2.6.0-docs-dogfood` branch shared across the three test repos
# (see reference_live_test_repos.md in project memory). This harness must
# never guess or fabricate a SHA -- fill this in by actually inspecting the
# branch (`git ls-remote` / provider API) before the first live `run`.
PINNED_SEED_SHA = "REPLACE_ME_PINNED_SHA"

# Marker file the harness commits on top of the pinned seed SHA to create a
# uniquely identifiable run commit. Content is just the run_id (see
# platforms.py:create_run_commit).
RUN_MARKER_PATH = ".e2e-run"


@dataclass(frozen=True)
class ExpectedFinding:
    """One deterministic analyzer finding a fixture is expected to produce.

    `path_substring` matches loosely against a finding's file path (not an
    exact match) since analyzer output can shift line numbers across runs
    without changing the underlying flaw. `category` matches
    ai_pr_review.findings.models' CATEGORIES vocabulary.
    """

    path_substring: str
    category: str


@dataclass(frozen=True)
class PlatformConfig:
    name: str
    repo_slug: str
    base_ref: str
    # Minimum distinct analyzer findings expected from the fixture diff, as a
    # sanity backstop -- NOT the old harness's >=10 threshold, which was
    # tuned against LLM-transcribed shell output and produced false
    # confidence. A low floor here just catches "the review produced nothing
    # at all", not "the review produced everything we expect".
    findings_floor: int = 3
    expected_findings: tuple[ExpectedFinding, ...] = field(default_factory=tuple)


# Bitbucket's base differs from GitHub/GitLab (main/master) because the
# Bitbucket test repo's default branch was never renamed off its original
# provider-parity feature branch when OpenAI-provider support landed there;
# rather than rename it (and disturb whatever else depends on that branch
# name), the old e2e script targeted `test/openai-provider-parity` directly
# as Bitbucket's base_ref, and this harness preserves that same choice for
# platform parity with the harness it replaces.
PLATFORMS: dict[str, PlatformConfig] = {
    "github": PlatformConfig(
        name="github",
        repo_slug="tag1consulting/ai-pr-review-test",
        base_ref="main",
        expected_findings=(
            # TODO: fill in from an actual inspection of the GitHub fixture
            # diff (tests/canary/corpus/ or the seeded dogfood branch) --
            # these two entries are plausible placeholders, not confirmed
            # real analyzer output. Do not treat as verified until replaced.
            ExpectedFinding(path_substring="docs/", category="documentation"),
            ExpectedFinding(path_substring=".py", category="security"),
        ),
    ),
    "gitlab": PlatformConfig(
        name="gitlab",
        repo_slug="tag1consulting/ai-pr-review-test",
        base_ref="master",
        expected_findings=(
            ExpectedFinding(path_substring="docs/", category="documentation"),
        ),
    ),
    "bitbucket": PlatformConfig(
        name="bitbucket",
        repo_slug="tag1consulting/ai-pr-review-test",
        base_ref="test/openai-provider-parity",
        expected_findings=(
            ExpectedFinding(path_substring="docs/", category="documentation"),
        ),
    ),
}

VALID_PLATFORM_NAMES = frozenset(PLATFORMS.keys())

# "release" profile: exactly these three platforms, no dupes/unknowns/empty.
RELEASE_PROFILE_PLATFORMS = frozenset({"github", "gitlab", "bitbucket"})

# provider -> (standard_model_id, premium_model_id), read live from
# ReviewConfig.resolve_models() rather than hardcoded.
DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "anthropic": _current_default_models("anthropic"),
}

DEFAULT_MODE = "full"
DEFAULT_MAX_COST_USD = 2.00
CONTAINER_TIMEOUT_SECONDS = 12 * 60
