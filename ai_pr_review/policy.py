"""Repo-local review-policy routing: .ai-pr-review/policy.yml.

Lets a consuming repo route review depth (agent/analyzer selection, review
mode) by changed-file glob, base-branch glob, or head-branch glob, instead
of hand-rolling a GitHub Actions expression per repo. See docs/policy.md
for the schema and the full precedence chain (explicit action inputs and
slash-command overrides still win over anything resolved here).

Path (issue #839 follow-up): the preferred location is the
provider-neutral ``.ai-pr-review/policy.yml``. ``.github/ai-pr-review/policy.yml``
is still read as a fallback when the neutral path is absent, for existing
GitHub adopters and for repos that have not migrated -- the two are never
merged, first match wins. The neutral path exists because this is an
engine-side feature that works identically on GitHub, GitLab, and Bitbucket
Pipelines (see docs/agents.md), yet the legacy path forces a Bitbucket or
GitLab repo to create a ``.github/`` directory it otherwise has no reason to
have. ``ai_pr_review.feedback.store`` already used the neutral convention
for the learning-loop store before this module adopted it.

Security (issue #869): by default the policy file is loaded from the PR's
*base* ref via ``git show origin/{base_ref}:.ai-pr-review/policy.yml``
(falling back to ``.github/ai-pr-review/policy.yml`` per the Path note
above) — never from the checked-out working tree, which on a PR is
attacker-controlled. A malicious PR must not be able to edit its own policy
file to disable review agents on itself. This is a stricter trust model than
the pre-existing ``.ai-pr-review/suppressions.json`` / ``.github/ai-pr-review/suppressions.json``
(loaded from the working tree by ``ai_pr_review.findings.suppress``), which
is a narrower, lower-severity gap tracked separately.

That base-ref read only closes a real gap when the *workflow itself* is
base-controlled, i.e. GitHub ``pull_request_target``. Under the far more
common ``pull_request`` event (the default in
``examples/workflows/pr-review.yml``), the workflow file already runs from
the PR head — a PR can already delete the review job, set
``exclude-agents``, or force ``review-mode: quick`` regardless of where
policy.yml is read from. In that case the base-ref read guards a side door
while the front door is already PR-controlled; its only effect is that a
policy change takes one extra merge to apply. ``AI_POLICY_SOURCE`` /
``policy-source`` (default ``base-ref``) lets a repo whose workflow is
head-controlled opt into ``workspace`` instead, reading from the checked-out
tree directly and skipping that extra-merge delay. Setting ``workspace``
under ``pull_request_target`` reopens the gap this module exists to close —
see docs/policy.md.

Fail-soft throughout: a missing policy file is the normal, expected case
for every repo that hasn't opted in and produces no warning. A malformed
file (bad YAML, unknown policy/route reference, cyclic 'extends', an
unconstrained catch-all route) prints a single WARNING to stderr and the
caller falls back to hardcoded engine defaults — a bad policy file must
never block a review.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import yaml

_POLICY_PATH = ".ai-pr-review/policy.yml"
_POLICY_PATH_LEGACY = ".github/ai-pr-review/policy.yml"
# First match wins. The two are never merged. See the module docstring.
_POLICY_PATH_CANDIDATES: tuple[str, ...] = (_POLICY_PATH, _POLICY_PATH_LEGACY)
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
    """One ordered route: the first rule whose ``when`` matches wins.

    ``require``, when set, names a policy that must have (transitively)
    run for this route's merge gate to pass — see
    ``ai_pr_review.vcs.github.GitHubProvider.post_check_run`` and
    ``policy_satisfies``. It plays no role in route matching or in
    resolve_policy(); it is read back out by the caller after the review
    completes, once the *actually applied* policy for this run is known.
    """

    policy: str
    paths: tuple[str, ...] = ()
    base_branch: str | None = None
    head_branch: str | None = None
    require: str | None = None


@dataclass(frozen=True)
class PolicyFile:
    version: int
    policies: dict[str, PolicyDef]
    routes: tuple[RouteRule, ...]
    default: str | None


@dataclass(frozen=True)
class ResolvedPolicy:
    """A named policy fully resolved through its 'extends' chain.

    ``agents``/``analyzers`` being an empty tuple is ambiguous on its own —
    it could mean "no restriction" (the built-in quick/full bases) or
    "explicitly restricted to zero" (a policy declaring ``agents: []``).
    ``agents_restricted``/``analyzers_restricted`` disambiguates: True
    means some policy in the extends chain explicitly set that field
    (regardless of the resulting value), so the caller must not treat an
    empty ``agents``/``analyzers`` here as "unset" — see
    ``ai_pr_review.review.runtime.build_review_runtime``'s merge, which
    translates an empty-but-restricted allow list into an explicit
    deny-all (an empty allow tuple means "permit everything" everywhere
    else in the config surface, so it can't carry this meaning on its
    own).
    """

    name: str
    review_mode: str
    agents: tuple[str, ...] = ()
    exclude_agents: tuple[str, ...] = ()
    analyzers: tuple[str, ...] = ()
    exclude_analyzers: tuple[str, ...] = ()
    agents_restricted: bool = False
    analyzers_restricted: bool = False


def load_policy_file(
    workspace: str, base_ref: str, *, source: str = "base-ref"
) -> PolicyFile | None:
    """Load and parse policy.yml. None on absence or error.

    ``source`` (issue #869): ``"base-ref"`` (default) reads via
    ``git show origin/{base_ref}:...`` as before — see this module's
    docstring for the trust-model rationale and when this actually matters.
    ``"workspace"`` reads directly from the checked-out working tree instead,
    for a repo whose workflow is already head-controlled (plain
    ``pull_request``) and would rather skip base-ref's one-extra-merge delay
    than pay for protection the workflow trigger doesn't provide anyway.
    Never use ``"workspace"`` under ``pull_request_target`` — that reopens
    the exact gap the base-ref read exists to close.

    A missing file is the common case (no policy adopted) and is silent in
    both modes. Any parse/validation failure prints one WARNING and returns
    None so the review proceeds with hardcoded defaults.
    """
    if source not in ("base-ref", "workspace"):
        print(
            f"WARNING: policy-source={source!r} is not 'base-ref' or 'workspace'; "
            "falling back to 'base-ref'.",
            file=sys.stderr,
        )
        source = "base-ref"

    if source == "workspace":
        raw_text, path_used = _read_policy_from_workspace(workspace)
    else:
        raw_text, path_used = _read_policy_from_base_ref(workspace, base_ref)
    if raw_text is None:
        return None
    assert path_used is not None  # non-None text always pairs with the path that produced it

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        print(f"WARNING: {path_used} is not valid YAML: {exc}", file=sys.stderr)
        return None
    if not isinstance(raw, dict):
        print(f"WARNING: {path_used} must be a YAML mapping; ignoring", file=sys.stderr)
        return None
    try:
        return _parse_policy_file(raw)
    except ValueError as exc:
        print(f"WARNING: {path_used} is invalid; ignoring: {exc}", file=sys.stderr)
        return None


def _read_first_existing(
    candidates: Sequence[str], read_one: Callable[[str], tuple[str | None, bool]]
) -> tuple[str | None, str | None]:
    """Try each candidate path in order via ``read_one``. First hit wins.

    ``read_one`` returns ``(content, had_real_error)``. A real error (not a
    clean "not found") stops the search immediately, returning ``(None,
    None)`` without trying later candidates -- matching this module's
    pre-existing single-path contract, where a WARNING meant the load
    failed, full stop. Without this, a real error on the preferred
    candidate (a git timeout, an unreadable file) could be silently masked
    by a clean load of the legacy candidate: the WARNING for the real
    failure would be followed by an INFO line that looks identical to the
    intended, benign "not yet migrated" case, with no way to tell the two
    apart from the logs and the wrong (stale) policy silently applied.

    Never merges content across candidates. Returns ``(content, path)`` on
    success, ``(None, None)`` when none of the candidates exist (or a real
    error stopped the search). Logs one INFO line only when the fallback
    (non-first) candidate was the one that resolved cleanly, so a repo
    still on the legacy path gets a nudge to migrate without every run on
    the preferred path printing anything.
    """
    for i, path in enumerate(candidates):
        content, had_error = read_one(path)
        if had_error:
            return None, None
        if content is not None:
            if i > 0:
                print(
                    f"INFO: loaded policy from legacy path {path!r}. "
                    f"The preferred location is {candidates[0]!r}.",
                    file=sys.stderr,
                )
            return content, path
    return None, None


def _read_policy_from_base_ref(workspace: str, base_ref: str) -> tuple[str | None, str | None]:
    """Read policy.yml's raw text via ``git show origin/{base_ref}:...``.

    Tries ``_POLICY_PATH_CANDIDATES`` in order (neutral path first, legacy
    fallback second). The two are never merged. Returns ``(None, None)``
    (silently, or with a WARNING for a real error) exactly as
    ``load_policy_file`` always has -- this is a pure extraction of the
    original single-path body, generalized to try a second path on a clean
    "not found".
    """
    if not base_ref:
        return None, None
    return _read_first_existing(
        _POLICY_PATH_CANDIDATES,
        lambda path: _git_show_policy_path(workspace, base_ref, path),
    )


def _git_show_policy_path(workspace: str, base_ref: str, path: str) -> tuple[str | None, bool]:
    try:
        proc = subprocess.run(
            ["git", "show", f"origin/{base_ref}:{path}"],
            cwd=workspace or ".",
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"WARNING: could not read {path} from origin/{base_ref}: {exc}",
            file=sys.stderr,
        )
        return None, True
    if proc.returncode != 0:
        # git show's fatal message for "the path isn't tracked at that ref"
        # is stable across git versions: "fatal: path '<path>' does not
        # exist in '<ref>'". That's the common, expected case for a repo
        # that hasn't adopted policy.yml (or hasn't adopted this particular
        # candidate path) and must stay silent -- and must let the caller
        # try the next candidate. Any other non-zero exit (bad/unknown ref,
        # unreachable remote, permissions) is a real misconfiguration
        # masquerading as "no policy adopted": worth a WARNING, and it must
        # stop the search rather than silently falling through to a later
        # candidate (see _read_first_existing's docstring for why).
        if "does not exist in" not in proc.stderr:
            print(
                f"WARNING: could not read {path} from origin/{base_ref} "
                f"(git show exited {proc.returncode}): {proc.stderr.strip()[:500]}",
                file=sys.stderr,
            )
            return None, True
        return None, False
    return proc.stdout, False


def _read_policy_from_workspace(workspace: str) -> tuple[str | None, str | None]:
    """Read policy.yml's raw text directly from the checked-out working tree.

    Tries ``_POLICY_PATH_CANDIDATES`` in order, same first-match-wins rule
    as the base-ref reader. A missing file is the common, silent case for
    each candidate. Any other read error (permission denied, not a regular
    file) is worth a WARNING -- it's a real misconfiguration, not "no
    policy adopted".
    """
    return _read_first_existing(
        _POLICY_PATH_CANDIDATES,
        lambda path: _read_workspace_policy_path(workspace, path),
    )


def _read_workspace_policy_path(workspace: str, path: str) -> tuple[str | None, bool]:
    full_path = os.path.join(workspace or ".", path)
    try:
        with open(full_path, encoding="utf-8") as f:
            return f.read(), False
    except FileNotFoundError:
        return None, False
    except OSError as exc:
        print(f"WARNING: could not read {full_path}: {exc}", file=sys.stderr)
        return None, True


def match_route(
    policy_file: PolicyFile,
    changed_files: Sequence[str],
    base_ref: str,
    head_ref: str,
) -> RouteRule | None:
    """Return the first matching route in full (including ``require``), or
    None when no route matches — the caller falls back to
    ``policy_file.default``, which carries no merge-gate requirement of its
    own (only an explicit route can set ``require``).
    """
    for route in policy_file.routes:
        if _route_matches(route, changed_files, base_ref, head_ref):
            return route
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
    route = match_route(policy_file, changed_files, base_ref, head_ref)
    return route.policy if route else policy_file.default


def policy_satisfies(policy_file: PolicyFile, ran_policy_name: str, required_policy_name: str) -> bool:
    """True if the policy that actually ran meets or exceeds ``required_policy_name``.

    Two ways to satisfy a requirement: the ran policy IS the required one
    (exact name match — handles two same-tier policies that aren't
    otherwise comparable), or the ran policy resolved to full review mode,
    which is a superset of any 'quick'-based tier by construction (full
    mode runs strictly more agents than quick mode).
    """
    if ran_policy_name == required_policy_name:
        return True
    return resolve_policy(policy_file, ran_policy_name).review_mode == "full"


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
    agents_restricted = False
    analyzers_restricted = False
    for pol in chain:
        if pol.agents is not None:
            agents = pol.agents
            agents_restricted = True
        if pol.exclude_agents is not None:
            exclude_agents = pol.exclude_agents
        if pol.analyzers is not None:
            analyzers = pol.analyzers
            analyzers_restricted = True
        if pol.exclude_analyzers is not None:
            exclude_analyzers = pol.exclude_analyzers

    return ResolvedPolicy(
        name=policy_name,
        review_mode=review_mode,
        agents=agents,
        exclude_agents=exclude_agents,
        analyzers=analyzers,
        exclude_analyzers=exclude_analyzers,
        agents_restricted=agents_restricted,
        analyzers_restricted=analyzers_restricted,
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
        require = _opt_str(r.get("require"), f"routes[{i}].require")
        if require is not None and require not in policies and require not in _BUILTIN_BASES:
            raise ValueError(f"routes[{i}].require references unknown policy {require!r}")
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
            RouteRule(
                policy=policy_name,
                paths=paths,
                base_branch=base_branch,
                head_branch=head_branch,
                require=require,
            )
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
    # _validate_analyzer_names_list (not just _validate_names_tuple) so a
    # deprecated-but-inert analyzer name (#815) referenced in a policy.yml
    # route is accepted with a warning here too, not just in the main
    # analyzers/exclude-analyzers env-var path -- one shared source of
    # truth for which analyzer names are tolerated post-deprecation.
    from ai_pr_review.config import _validate_analyzer_names_list  # noqa: PLC0415

    try:
        return _validate_analyzer_names_list(names)
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
