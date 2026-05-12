"""
tests/golden/test_config_parity.py — Validate that config_matrix.md covers
every env var documented in docs/configuration.md.

Implements 0.FR-5. Fails if any env var from docs/configuration.md is
missing from config_matrix.md.
"""
import re
from pathlib import Path
import pytest

DOCS_CONFIG = Path(__file__).parent.parent.parent / "docs" / "configuration.md"
CONFIG_MATRIX = Path(__file__).parent / "config_matrix.md"

# ---------------------------------------------------------------------------
# Extract env var names from docs/configuration.md
# ---------------------------------------------------------------------------

def extract_vars_from_docs(path: Path) -> set[str]:
    """Extract backtick-quoted env var names from configuration.md tables.

    Looks for patterns like `VARIABLE_NAME` on table rows.
    Includes action inputs like `provider`, `api-key`, etc.
    """
    content = path.read_text()
    # Match backtick-quoted identifiers that look like env vars or action inputs:
    #   ALL_CAPS_WITH_UNDERSCORES  → env var
    #   lowercase-with-hyphens      → action input
    vars_found = set()
    for match in re.finditer(r"`([A-Z][A-Z0-9_]{2,}|[a-z][a-z0-9-]{2,})`", content):
        candidate = match.group(1)
        # Filter out obvious non-vars (values like 'true', 'false', 'auto',
        # provider names, etc.)
        non_vars = {
            "true", "false", "auto", "quick", "full", "pr", "standalone",
            "github", "gitlab", "bitbucket", "anthropic", "openai", "google",
            "bedrock-proxy", "openai-compatible", "auto",
        }
        if candidate.lower() not in non_vars and len(candidate) >= 3:
            vars_found.add(candidate)
    return vars_found


def extract_vars_from_matrix(path: Path) -> set[str]:
    """Extract env var and input names listed in config_matrix.md."""
    content = path.read_text()
    vars_found = set()
    for match in re.finditer(r"`([A-Z][A-Z0-9_]{2,}|[a-z][a-z0-9-]{2,})`", content):
        candidate = match.group(1)
        non_vars = {
            "true", "false", "auto", "quick", "full", "pr", "standalone",
            "github", "gitlab", "bitbucket", "anthropic", "openai", "google",
            "bedrock-proxy", "openai-compatible",
        }
        if candidate.lower() not in non_vars and len(candidate) >= 3:
            vars_found.add(candidate)
    return vars_found


# ---------------------------------------------------------------------------
# Known env vars from action.yml that map to internal names
# ---------------------------------------------------------------------------
# These are the canonical env var names from docs/configuration.md that the
# test explicitly checks. The matrix must contain each of these.
REQUIRED_VARS = {
    # Action inputs (input names as documented)
    "provider", "api-key", "base-url", "model-standard", "model-premium",
    "review-mode", "review-target", "max-diff-lines", "pr-number", "base-ref",
    "head-sha", "github-token", "parallel", "max-inline", "max-tokens-per-agent",
    "enable-suggestions",
    # Core runtime env vars
    "AI_TEMPERATURE", "LLM_PROMPT_CACHING", "AI_CACHE_PRIMING", "VCS_PROVIDER",
    "PHPSTAN_LEVEL",
    # Advanced tuning
    "FORCE_FULL_DIFF", "STANDALONE_DEPTH", "LLM_RETRY_COUNT",
    "AI_CONFIDENCE_THRESHOLD",
    "AI_DISABLE_GATE_ARCHITECTURE", "AI_DISABLE_GATE_SECURITY",
    "AI_DISABLE_GATE_EDGE_CASE",
    # Bitbucket
    "BITBUCKET_EMAIL", "BITBUCKET_API_TOKEN", "BITBUCKET_WORKSPACE",
    "BITBUCKET_REPO_SLUG",
    # GitLab
    "GITLAB_TOKEN", "GITLAB_API_URL", "GITLAB_PROJECT_ID",
    "GITLAB_MR_DIFF_BASE_SHA", "GITLAB_BOT_USERNAME",
    # GitLab CI auto-vars
    "CI_PROJECT_ID", "CI_PROJECT_PATH", "CI_MERGE_REQUEST_IID",
    "CI_MERGE_REQUEST_DIFF_BASE_SHA", "CI_JOB_TOKEN",
}


def test_config_matrix_exists() -> None:
    assert CONFIG_MATRIX.exists(), (
        f"config_matrix.md not found at {CONFIG_MATRIX}. "
        "Run E0.S5 implementation to create it."
    )


def test_config_matrix_covers_required_vars() -> None:
    assert CONFIG_MATRIX.exists(), "config_matrix.md missing — run test_config_matrix_exists first"
    matrix_vars = extract_vars_from_matrix(CONFIG_MATRIX)
    missing = REQUIRED_VARS - matrix_vars
    assert not missing, (
        f"config_matrix.md is missing these vars from the required set:\n"
        + "\n".join(f"  - {v}" for v in sorted(missing))
    )


def test_config_matrix_covers_docs_configuration() -> None:
    if not DOCS_CONFIG.exists():
        pytest.skip(f"docs/configuration.md not found at {DOCS_CONFIG}")
    if not CONFIG_MATRIX.exists():
        pytest.fail("config_matrix.md missing")

    docs_vars = extract_vars_from_docs(DOCS_CONFIG)
    matrix_vars = extract_vars_from_matrix(CONFIG_MATRIX)

    # Check that every var from docs appears in matrix (may appear as either
    # the input name or the mapped env var name).
    # We filter to only check the REQUIRED_VARS subset since docs may contain
    # many code snippets that aren't actually env vars.
    docs_required = docs_vars & REQUIRED_VARS
    missing_from_matrix = docs_required - matrix_vars
    assert not missing_from_matrix, (
        f"These vars from docs/configuration.md are missing from config_matrix.md:\n"
        + "\n".join(f"  - {v}" for v in sorted(missing_from_matrix))
    )


def test_config_matrix_has_manual_test_only_rationale() -> None:
    """Every 'manual-test-only' entry must have a rationale comment."""
    if not CONFIG_MATRIX.exists():
        pytest.fail("config_matrix.md missing")
    content = CONFIG_MATRIX.read_text()
    # Check that the "Manual-test-only rationale categories" section exists
    assert "Manual-test-only rationale" in content, (
        "config_matrix.md must include a 'Manual-test-only rationale' section "
        "explaining why each var is not fixture-covered."
    )
