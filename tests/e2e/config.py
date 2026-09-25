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
# Pinned seed commits
# ---------------------------------------------------------------------------
#
# One SHA per platform, NOT a single shared constant: GitHub, GitLab, and
# Bitbucket are three independent repositories. Even where the seed branch
# holds content-identical commits across all three (it does here -- the same
# fixture history was pushed to each), each provider computes its own commit
# hash, so the three SHAs are genuinely different values. An earlier version
# of this file had a single PINNED_SEED_SHA used for all three adapters; it
# was only ever verified against GitHub, and the first live GitLab run failed
# immediately with "404 Commit Not Found" as a direct result. Each value
# below was independently verified via that platform's own API (branch tip
# lookup, plus a compare/diff call confirming a clean ahead-of relationship
# to that platform's own base_ref) before being pinned here.

# Verified via `gh api repos/tag1consulting/ai-pr-review-test/git/refs/heads/
# test/v2.6.0-docs-dogfood` on 2026-09-25 -- 2 commits ahead of `main`, clean
# merge-base equal to `main`'s own tip.
GITHUB_SEED_SHA = "4851571360ad7ad6d23ad4c99ff47ea8408106a8"

# Verified via GitLab's branches API + compare?from=master&to=... on
# 2026-09-25 -- 4 commits ahead of `master`, clean linear-ahead diff.
GITLAB_SEED_SHA = "256f09dc2e669adc9feb8270e1c4f964568b3962"

# Verified via Bitbucket's commits API (excluding test/openai-provider-parity)
# on 2026-09-25 -- 5 commits ahead of that base_ref, clean linear-ahead diff.
BITBUCKET_SEED_SHA = "f6eea6bb463ab720408f07432daa2f57604a8879"

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
    seed_sha: str
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
        seed_sha=GITHUB_SEED_SHA,
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
        seed_sha=GITLAB_SEED_SHA,
        expected_findings=(
            ExpectedFinding(path_substring="docs/", category="documentation"),
        ),
    ),
    "bitbucket": PlatformConfig(
        name="bitbucket",
        # NOT tag1consulting -- the Bitbucket test repo lives in a different
        # workspace than the GitHub/GitLab ones. Verified against the old
        # harness (ai-pr-review-e2e.js) and project memory
        # (reference_live_test_repos.md); an earlier version of this file
        # had this wrong, which would have 404'd every Bitbucket API call.
        repo_slug="gchaix-tag1/ai-pr-review-test",
        base_ref="test/openai-provider-parity",
        seed_sha=BITBUCKET_SEED_SHA,
        # Verified live against this fixture's real posted Code Insights
        # annotations on 2026-09-25 (25 annotations, all security findings --
        # no docs/ finding was ever produced, unlike the placeholder this
        # replaces). api/user.py has 4 separate injection findings (F1-F4).
        expected_findings=(
            ExpectedFinding(path_substring="api/user.py", category="injection"),
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
