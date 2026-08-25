"""Repo-local review-policy routing: .github/ai-pr-review/policy.yml.

Lets a consuming repo route review depth (agent/analyzer selection, review
mode) by changed-file glob, base-branch glob, or head-branch glob, instead
of hand-rolling a GitHub Actions expression per repo. See docs/policy.md
for the schema and the full precedence chain (explicit action inputs and
slash-command overrides still win over anything resolved here).

Security: the policy file is loaded from the PR's *base* ref via
``git show origin/{base_ref}:.github/ai-pr-review/policy.yml`` — never from
the checked-out working tree, which on a PR is attacker-controlled. A
malicious PR must not be able to edit its own policy file to disable review
agents on itself. This is a stricter trust model than the pre-existing
``.github/ai-pr-review/suppressions.json`` (loaded from the working tree by
``ai_pr_review.findings.suppress``), which is a narrower, lower-severity
gap tracked separately.

Fail-soft throughout: a missing policy file is the normal, expected case
for every repo that hasn't opted in and produces no warning. A malformed
file (bad YAML, unknown policy/route reference, cyclic 'extends', an
unconstrained catch-all route) prints a single WARNING to stderr and the
caller falls back to hardcoded engine defaults — a bad policy file must
never block a review.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import yaml

_POLICY_PATH = ".github/ai-pr-review/policy.yml"
_BUILTIN_BASES: frozenset[str] = frozenset({"quick", "full"})
_GIT_TIMEOUT_SECS = 15
_WHEN_KEYS: frozenset[str] = frozenset({"paths", "base-branch", "head-branch"})


@dataclass(frozen=True)
class PolicyDef:
    """One named policy. ``None`` fields inherit from ``extends``."""

    name: str
    extends: str | None = None
    agents: tuple[str, ...] | None = None
    exclude_agents: tuple[str, ...] | None = None
    analyzers: tuple[str, ...] | None = None
    exclude_analyzers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RouteRule:
    """One ordered route: the first rule whose ``when`` matches wins."""

    policy: str
    paths: tuple[str, ...] = ()
    base_branch: str | None = None
    head_branch: str | None = None


@dataclass(frozen=True)
class PolicyFile:
    version: int
    policies: dict[str, PolicyDef]
    routes: tuple[RouteRule, ...]
    default: str | None


@dataclass(frozen=True)
class ResolvedPolicy:
    """A named policy fully resolved through its 'extends' chain."""

    name: str
    review_mode: str
    agents: tuple[str, ...] = ()
    exclude_agents: tuple[str, ...] = ()
    analyzers: tuple[str, ...] = ()
    exclude_analyzers: tuple[str, ...] = ()


def load_policy_file(workspace: str, base_ref: str) -> PolicyFile | None:
    """Load and parse policy.yml from the base ref. None on absence or error.

    A missing file at that ref is the common case (no policy adopted) and
    is silent. Any parse/validation failure prints one WARNING and returns
    None so the review proceeds with hardcoded defaults.
    """
    if not base_ref:
        return None
    try:
        proc = subprocess.run(
            ["git", "show", f"origin/{base_ref}:{_POLICY_PATH}"],
            cwd=workspace or ".",
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"WARNING: could not read {_POLICY_PATH} from origin/{base_ref}: {exc}",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        # No policy.yml at that ref (or the ref/remote is unavailable) — the
        # expected state for a repo that hasn't opted in. Not an error.
        return None
    try:
        raw = yaml.safe_load(proc.stdout)
    except yaml.YAMLError as exc:
        print(f"WARNING: {_POLICY_PATH} is not valid YAML: {exc}", file=sys.stderr)
        return None
    if not isinstance(raw, dict):
        print(f"WARNING: {_POLICY_PATH} must be a YAML mapping; ignoring", file=sys.stderr)
        return None
    try:
        return _parse_policy_file(raw)
    except ValueError as exc:
        print(f"WARNING: {_POLICY_PATH} is invalid; ignoring: {exc}", file=sys.stderr)
        return None


def resolve_route(
    policy_file: PolicyFile,
    changed_files: Sequence[str],
    base_ref: str,
    head_ref: str,
) -> str | None:
    """Return the name of the first matching route's policy.

    Falls back to ``policy_file.default`` when no route matches, and to
    None (caller uses hardcoded defaults) when there is no default either.
    """
    for route in policy_file.routes:
        if _route_matches(route, changed_files, base_ref, head_ref):
            return route.policy
    return policy_file.default


def resolve_policy(policy_file: PolicyFile, policy_name: str) -> ResolvedPolicy:
    """Resolve a named policy by walking its 'extends' chain to a built-in base.

    Each policy's own non-None fields override its parent's; an unset field
    inherits the parent's resolved value. ``load_policy_file`` already
    rejects unresolvable/cyclic chains, so this only re-detects a cycle
    defensively (unreachable in practice via the public loader).
    """
    if policy_name in _BUILTIN_BASES:
        return ResolvedPolicy(name=policy_name, review_mode=policy_name)

    chain: list[PolicyDef] = []
    name = policy_name
    seen: set[str] = set()
    while name not in _BUILTIN_BASES:
        if name in seen:
            raise ValueError(f"cyclic 'extends' chain involving {name!r}")
        seen.add(name)
        pol = policy_file.policies[name]
        chain.append(pol)
        name = pol.extends or "quick"
    review_mode = name
    chain.reverse()  # root-to-leaf application order

    agents: tuple[str, ...] = ()
    exclude_agents: tuple[str, ...] = ()
    analyzers: tuple[str, ...] = ()
    exclude_analyzers: tuple[str, ...] = ()
    for pol in chain:
        if pol.agents is not None:
            agents = pol.agents
        if pol.exclude_agents is not None:
            exclude_agents = pol.exclude_agents
        if pol.analyzers is not None:
            analyzers = pol.analyzers
        if pol.exclude_analyzers is not None:
            exclude_analyzers = pol.exclude_analyzers

    return ResolvedPolicy(
        name=policy_name,
        review_mode=review_mode,
        agents=agents,
        exclude_agents=exclude_agents,
        analyzers=analyzers,
        exclude_analyzers=exclude_analyzers,
    )


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def _parse_policy_file(raw: dict[object, object]) -> PolicyFile:
    version = raw.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported policy.yml version {version!r} (only 1 is supported)")

    raw_policies = raw.get("policies") or {}
    if not isinstance(raw_policies, dict):
        raise ValueError("'policies' must be a mapping")
    policies: dict[str, PolicyDef] = {}
    for name, body in raw_policies.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"policy name must be a non-empty string, got {name!r}")
        if name in _BUILTIN_BASES:
            raise ValueError(
                f"policy name {name!r} collides with a built-in base ('quick'/'full')"
            )
        if not isinstance(body, dict):
            raise ValueError(f"policies.{name} must be a mapping")
        policies[name] = PolicyDef(
            name=name,
            extends=_opt_str(body.get("extends"), f"policies.{name}.extends"),
            agents=_opt_agent_names(body.get("agents"), f"policies.{name}.agents"),
            exclude_agents=_opt_agent_names(
                body.get("exclude-agents"), f"policies.{name}.exclude-agents"
            ),
            analyzers=_opt_analyzer_names(body.get("analyzers"), f"policies.{name}.analyzers"),
            exclude_analyzers=_opt_analyzer_names(
                body.get("exclude-analyzers"), f"policies.{name}.exclude-analyzers"
            ),
        )

    # Validate every declared policy's 'extends' chain resolves and contains
    # no cycles up front, so a bad file is rejected wholesale rather than
    # failing unpredictably later depending on which route happened to match.
    for name in policies:
        _validate_extends_chain(policies, name)

    raw_routes = raw.get("routes") or []
    if not isinstance(raw_routes, list):
        raise ValueError("'routes' must be a list")
    routes: list[RouteRule] = []
    for i, r in enumerate(raw_routes):
        if not isinstance(r, dict):
            raise ValueError(f"routes[{i}] must be a mapping")
        policy_name = r.get("policy")
        if not isinstance(policy_name, str) or not policy_name:
            raise ValueError(f"routes[{i}].policy must be a non-empty string")
        if policy_name not in policies and policy_name not in _BUILTIN_BASES:
            raise ValueError(f"routes[{i}].policy references unknown policy {policy_name!r}")
        when = r.get("when") or {}
        if not isinstance(when, dict):
            raise ValueError(f"routes[{i}].when must be a mapping")
        unknown_keys = set(when) - _WHEN_KEYS
        if unknown_keys:
            raise ValueError(f"routes[{i}].when has unknown key(s): {sorted(unknown_keys)}")
        paths = _opt_str_list(when.get("paths"), f"routes[{i}].when.paths") or ()
        base_branch = _opt_str(when.get("base-branch"), f"routes[{i}].when.base-branch")
        head_branch = _opt_str(when.get("head-branch"), f"routes[{i}].when.head-branch")
        if not paths and base_branch is None and head_branch is None:
            raise ValueError(
                f"routes[{i}].when must constrain at least one of "
                "paths/base-branch/head-branch (an unconstrained route "
                "would match every PR, silently shadowing every route after it)"
            )
        routes.append(
            RouteRule(policy=policy_name, paths=paths, base_branch=base_branch, head_branch=head_branch)
        )

    default = raw.get("default")
    if default is not None:
        if not isinstance(default, str) or not default:
            raise ValueError("'default' must be a non-empty string")
        if default not in policies and default not in _BUILTIN_BASES:
            raise ValueError(f"'default' references unknown policy {default!r}")

    return PolicyFile(version=version, policies=policies, routes=tuple(routes), default=default)


def _validate_extends_chain(
    policies: dict[str, PolicyDef], start_name: str, _visiting: frozenset[str] = frozenset()
) -> None:
    name = start_name
    visiting = set(_visiting)
    while name not in _BUILTIN_BASES:
        if name in visiting:
            raise ValueError(f"cyclic 'extends' chain involving {name!r}")
        visiting.add(name)
        pol = policies.get(name)
        if pol is None:
            raise ValueError(f"policy {name!r} not found (referenced via 'extends')")
        name = pol.extends or "quick"


def _opt_str(v: object, ctx: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not v:
        raise ValueError(f"{ctx} must be a non-empty string, got {v!r}")
    return v


def _opt_str_list(v: object, ctx: str) -> tuple[str, ...] | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ValueError(f"{ctx} must be a list of non-empty strings, got {v!r}")
    return tuple(v)


def _opt_agent_names(v: object, ctx: str) -> tuple[str, ...] | None:
    names = _opt_str_list(v, ctx)
    if not names:
        return names
    from ai_pr_review.agents.roster import AGENT_NAMES  # noqa: PLC0415
    from ai_pr_review.config import _validate_names_tuple  # noqa: PLC0415

    try:
        return _validate_names_tuple(names, AGENT_NAMES, "agent")
    except ValueError as exc:
        raise ValueError(f"{ctx}: {exc}") from exc


def _opt_analyzer_names(v: object, ctx: str) -> tuple[str, ...] | None:
    names = _opt_str_list(v, ctx)
    if not names:
        return names
    from ai_pr_review.analyzers.bridge import ANALYZER_NAMES  # noqa: PLC0415
    from ai_pr_review.config import _validate_names_tuple  # noqa: PLC0415

    try:
        return _validate_names_tuple(names, ANALYZER_NAMES, "analyzer")
    except ValueError as exc:
        raise ValueError(f"{ctx}: {exc}") from exc


def _route_matches(
    route: RouteRule, changed_files: Sequence[str], base_ref: str, head_ref: str
) -> bool:
    if route.paths and not any(
        fnmatch.fnmatch(f, pat) for f in changed_files for pat in route.paths
    ):
        return False
    if route.base_branch is not None and not fnmatch.fnmatch(base_ref, route.base_branch):
        return False
    return route.head_branch is None or fnmatch.fnmatch(head_ref or "", route.head_branch)
