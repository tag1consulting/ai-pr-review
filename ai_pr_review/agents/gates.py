"""Conditional gate evaluation for selective agent dispatch.

Gates inspect diff text and file paths to determine which agents run for a
given PR — cheap heuristic filters that keep expensive agents (security,
architecture, edge-case) off diffs that can't benefit from them (e.g.,
docs-only or pure-comment changes).

Kill-switch env vars have inverted semantics from what the names suggest:
setting `AI_DISABLE_GATE_ARCHITECTURE=true` does NOT skip the
architecture-reviewer agent — it DISABLES the gate check itself, so the
gate always fires and the gated agent always runs. This is a debugging /
testing override. There is no built-in switch to *skip* a gated agent;
for that, remove the agent from the roster.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ai_pr_review.agents.roster import AgentSpec, ConditionalTrigger
from ai_pr_review.manifest import ChangedFiles

# ---------------------------------------------------------------------------
# Compiled patterns (module-level for reuse)
# ---------------------------------------------------------------------------

_ERROR_PATTERN = re.compile(
    r"catch|if err|try \{|rescue|Result<|unwrap|except|\.catch\("
)

# Docs-only / meta exclusion patterns (architecture gate)
_WORKFLOW_PATH = re.compile(r"(^|/)\.github/workflows/")
_DOC_EXT = re.compile(r"\.(md|markdown|txt|rst|adoc)$")
_META_DIR = re.compile(r"(^|/)(docs|memory-bank|\.github|\.claude)/")
_META_FILENAME = re.compile(
    r"(^|/)(CHANGELOG|README|LICENSE|NOTICE|AUTHORS|CONTRIBUTING|CODEOWNERS|CODE_OF_CONDUCT)(\..+)?$"
)

# Security keyword regex (case-insensitive)
_SEC_KEYWORD = re.compile(
    r"auth|token|secret|password|crypt|hash|\bsign\b|verify|exec|eval|sql|"
    r"sanitize|escape|xss|csrf|cors|header|redirect|deserialize|cookie|session|"
    r"jwt|oauth|ldap|saml|rbac|acl|permission|privilege|sudo|chmod|chown|setuid|"
    r"x509|tls|ssl|cert|certificate|keystore|nonce|salt|hmac|aes|rsa|ecdsa|"
    r"pbkdf2|bcrypt|scrypt|curl|wget|\bsource\b|\bIFS\b|LD_PRELOAD|\$\{\{",
    re.IGNORECASE,
)

_SEC_PATH = re.compile(
    r"(auth|passwords?|credentials?|tokens?|secrets?)"
    r"|(^|/)(?:api|routes?)/"
    r"|(^|/)(?:package\.json|package-lock\.json|go\.mod|go\.sum|"
    r"composer\.json|composer\.lock|requirements[^/]*\.txt|pyproject\.toml|"
    r"Pipfile(?:\.lock)?|Gemfile(?:\.lock)?|[Cc]argo\.(?:toml|lock)|"
    r"yarn\.lock|pnpm-lock\.yaml)$"
    r"|(^|/)\.env"
    r"|(^|/)settings\.(?:py|ya?ml|json|toml)$"
    r"|(^|/)(?:Dockerfile|Containerfile)"
    r"|\.(?:sh|bash)$"
    r"|(^|/)\.github/workflows/"
)

# Control-flow keywords (word-boundary, added lines only)
_CONTROL_FLOW = re.compile(
    r"\b(?:if|elif|else|for|while|do|case|switch|match|try|catch|except|rescue|"
    r"unless|when|loop|break|continue|return|goto|defer|finally)\b"
)


# ---------------------------------------------------------------------------
# Individual gate functions
# ---------------------------------------------------------------------------

def _has_error_patterns(diff_text: str) -> bool:
    return bool(_ERROR_PATTERN.search(diff_text))


def _has_code_or_infra(changed_files: ChangedFiles) -> bool:
    """Fire when any changed file is code, config, or infra (single-pass).

    Workflow files under `.github/workflows/` count as infra. Docs-only
    (`.md`, `.rst`, `.txt`, `.adoc`), meta directories (`docs/`, `memory-bank/`,
    non-workflow `.github/`, `.claude/`), and meta filenames (CHANGELOG,
    README, LICENSE, etc.) are excluded.
    """
    for f in changed_files.all_files:
        if _WORKFLOW_PATH.search(f):
            return True
        if _DOC_EXT.search(f):
            continue
        if _META_DIR.search(f):
            continue
        if _META_FILENAME.search(f):
            continue
        return True
    return False


def _has_security_patterns(diff_text: str, changed_files: ChangedFiles) -> bool:
    if _SEC_KEYWORD.search(diff_text):
        return True
    return any(_SEC_PATH.search(f) for f in changed_files.all_files)


def _has_control_flow(diff_text: str) -> bool:
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if _CONTROL_FLOW.search(line):
            return True
    return False


def _no_prior_summary(last_reviewed_sha: str | None) -> bool:
    return not last_reviewed_sha


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_gates(
    diff_text: str,
    changed_files: ChangedFiles,
    env: Mapping[str, str],
    last_reviewed_sha: str | None = None,
) -> frozenset[ConditionalTrigger]:
    """Return the set of gate trigger keys whose conditions are met."""
    fired: set[ConditionalTrigger] = set()

    if _has_error_patterns(diff_text):
        fired.add("has_error_patterns")

    if env.get("AI_DISABLE_GATE_ARCHITECTURE", "").lower() in ("true", "1") or _has_code_or_infra(changed_files):
        fired.add("has_code_or_infra")

    if env.get("AI_DISABLE_GATE_SECURITY", "").lower() in ("true", "1") or _has_security_patterns(diff_text, changed_files):
        fired.add("has_security_patterns")

    if env.get("AI_DISABLE_GATE_EDGE_CASE", "").lower() in ("true", "1") or _has_control_flow(diff_text):
        fired.add("has_control_flow")

    if _no_prior_summary(last_reviewed_sha):
        fired.add("no_prior_summary")

    return frozenset(fired)


def filter_agents(
    agents: list[AgentSpec],
    fired_gates: frozenset[ConditionalTrigger],
) -> list[AgentSpec]:
    """Return agents whose conditional_trigger is None or in fired_gates."""
    return [
        a for a in agents
        if a.conditional_trigger is None or a.conditional_trigger in fired_gates
    ]
