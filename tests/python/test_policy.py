"""Tests for ai_pr_review.policy (repo-local .github/ai-pr-review/policy.yml)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_pr_review.policy import (
    PolicyDef,
    PolicyFile,
    RouteRule,
    _parse_policy_file,
    load_policy_file,
    resolve_policy,
    resolve_route,
)

# ---------------------------------------------------------------------------
# _parse_policy_file
# ---------------------------------------------------------------------------


def test_parse_minimal_valid_file() -> None:
    raw = {
        "version": 1,
        "policies": {"feature": {"extends": "quick"}},
        "routes": [{"when": {"head-branch": "feature/*"}, "policy": "feature"}],
        "default": "feature",
    }
    pf = _parse_policy_file(raw)
    assert pf.version == 1
    assert pf.policies["feature"] == PolicyDef(name="feature", extends="quick")
    assert pf.routes == (RouteRule(policy="feature", head_branch="feature/*"),)
    assert pf.default == "feature"


def test_parse_no_extends_implies_quick_base() -> None:
    raw = {
        "policies": {"content": {"agents": []}},
        "routes": [{"when": {"paths": ["docs/**"]}, "policy": "content"}],
    }
    pf = _parse_policy_file(raw)
    resolved = resolve_policy(pf, "content")
    assert resolved.review_mode == "quick"
    assert resolved.agents == ()


def test_parse_rejects_unsupported_version() -> None:
    with pytest.raises(ValueError, match="unsupported policy.yml version"):
        _parse_policy_file({"version": 2, "policies": {}, "routes": []})


def test_parse_rejects_policy_name_colliding_with_builtin() -> None:
    with pytest.raises(ValueError, match="collides with a built-in base"):
        _parse_policy_file({"policies": {"quick": {}}, "routes": []})


def test_parse_rejects_unknown_extends_target() -> None:
    with pytest.raises(ValueError, match="not found"):
        _parse_policy_file({"policies": {"a": {"extends": "nonexistent"}}, "routes": []})


def test_parse_rejects_cyclic_extends() -> None:
    raw = {
        "policies": {
            "a": {"extends": "b"},
            "b": {"extends": "a"},
        },
        "routes": [],
    }
    with pytest.raises(ValueError, match="cyclic"):
        _parse_policy_file(raw)


def test_parse_rejects_unknown_route_policy_reference() -> None:
    raw = {"policies": {}, "routes": [{"when": {"paths": ["*"]}, "policy": "nonexistent"}]}
    with pytest.raises(ValueError, match="unknown policy"):
        _parse_policy_file(raw)


def test_parse_rejects_unconstrained_route() -> None:
    """A route with no when-constraints would silently match every PR."""
    raw = {"policies": {"x": {}}, "routes": [{"when": {}, "policy": "x"}]}
    with pytest.raises(ValueError, match="must constrain at least one"):
        _parse_policy_file(raw)


def test_parse_rejects_unknown_when_key() -> None:
    raw = {"policies": {"x": {}}, "routes": [{"when": {"branch": "main"}, "policy": "x"}]}
    with pytest.raises(ValueError, match="unknown key"):
        _parse_policy_file(raw)


def test_parse_rejects_unknown_default_reference() -> None:
    raw = {"policies": {}, "routes": [], "default": "nonexistent"}
    with pytest.raises(ValueError, match="unknown policy"):
        _parse_policy_file(raw)


def test_parse_rejects_unknown_agent_name_with_suggestion() -> None:
    raw = {"policies": {"x": {"agents": ["cod-reviewer"]}}, "routes": []}
    with pytest.raises(ValueError, match="Did you mean 'code-reviewer'"):
        _parse_policy_file(raw)


def test_parse_rejects_unknown_analyzer_name() -> None:
    raw = {"policies": {"x": {"analyzers": ["not-a-real-analyzer"]}}, "routes": []}
    with pytest.raises(ValueError, match="Unknown analyzer name"):
        _parse_policy_file(raw)


def test_parse_accepts_extends_chain_to_named_policy() -> None:
    raw = {
        "policies": {
            "base-integration": {"extends": "quick", "agents": ["code-reviewer"]},
            "staging-smoke": {"extends": "base-integration"},
        },
        "routes": [],
    }
    pf = _parse_policy_file(raw)
    resolved = resolve_policy(pf, "staging-smoke")
    assert resolved.review_mode == "quick"
    assert resolved.agents == ("code-reviewer",)


# ---------------------------------------------------------------------------
# resolve_policy
# ---------------------------------------------------------------------------


def test_resolve_policy_builtin_quick() -> None:
    pf = PolicyFile(version=1, policies={}, routes=(), default=None)
    resolved = resolve_policy(pf, "quick")
    assert resolved.name == "quick"
    assert resolved.review_mode == "quick"
    assert resolved.agents == ()


def test_resolve_policy_builtin_full() -> None:
    pf = PolicyFile(version=1, policies={}, routes=(), default=None)
    resolved = resolve_policy(pf, "full")
    assert resolved.review_mode == "full"


def test_resolve_policy_extends_full_with_override() -> None:
    pf = PolicyFile(
        version=1,
        policies={
            "deep": PolicyDef(name="deep", extends="full", exclude_agents=("blind-hunter",))
        },
        routes=(),
        default=None,
    )
    resolved = resolve_policy(pf, "deep")
    assert resolved.review_mode == "full"
    assert resolved.exclude_agents == ("blind-hunter",)


def test_resolve_policy_leaf_overrides_parent() -> None:
    """A more-derived policy's own field wins over its parent's."""
    pf = PolicyFile(
        version=1,
        policies={
            "parent": PolicyDef(name="parent", extends="quick", agents=("code-reviewer",)),
            "child": PolicyDef(name="child", extends="parent", agents=("edge-case-hunter",)),
        },
        routes=(),
        default=None,
    )
    resolved = resolve_policy(pf, "child")
    assert resolved.agents == ("edge-case-hunter",)


def test_resolve_policy_child_inherits_unset_field() -> None:
    """A field the child leaves unset (None) inherits the parent's value."""
    pf = PolicyFile(
        version=1,
        policies={
            "parent": PolicyDef(name="parent", extends="quick", analyzers=("ruff",)),
            "child": PolicyDef(name="child", extends="parent"),
        },
        routes=(),
        default=None,
    )
    resolved = resolve_policy(pf, "child")
    assert resolved.analyzers == ("ruff",)


# ---------------------------------------------------------------------------
# resolve_route
# ---------------------------------------------------------------------------


def _pf(routes: tuple[RouteRule, ...], default: str | None = None) -> PolicyFile:
    return PolicyFile(version=1, policies={}, routes=routes, default=default)


def test_resolve_route_path_glob_match() -> None:
    pf = _pf((RouteRule(policy="content", paths=("docs/**", "*.md")),))
    assert resolve_route(pf, ["docs/index.md"], "main", "") == "content"
    assert resolve_route(pf, ["src/app.py"], "main", "") is None


def test_resolve_route_base_branch_glob_match() -> None:
    pf = _pf((RouteRule(policy="integration", base_branch="staging-*"),))
    assert resolve_route(pf, [], "staging-1.2", "") == "integration"
    assert resolve_route(pf, [], "main", "") is None


def test_resolve_route_head_branch_glob_match() -> None:
    pf = _pf((RouteRule(policy="feature", head_branch="feature/*"),))
    assert resolve_route(pf, [], "main", "feature/foo") == "feature"
    assert resolve_route(pf, [], "main", "hotfix/foo") is None


def test_resolve_route_head_branch_empty_never_matches_specific_glob() -> None:
    """A specific head-branch glob never matches when head_ref is unavailable
    (e.g. GitLab/Bitbucket, or an older consumer that hasn't wired head-ref).
    """
    pf = _pf((RouteRule(policy="feature", head_branch="feature/*"),))
    assert resolve_route(pf, [], "main", "") is None


def test_resolve_route_first_match_wins() -> None:
    pf = _pf(
        (
            RouteRule(policy="content", paths=("docs/**",)),
            RouteRule(policy="feature", paths=("**",)),
        )
    )
    assert resolve_route(pf, ["docs/index.md"], "main", "") == "content"


def test_resolve_route_falls_back_to_default() -> None:
    pf = _pf((RouteRule(policy="content", paths=("docs/**",)),), default="feature")
    assert resolve_route(pf, ["src/app.py"], "main", "") == "feature"


def test_resolve_route_no_match_no_default_returns_none() -> None:
    pf = _pf((RouteRule(policy="content", paths=("docs/**",)),))
    assert resolve_route(pf, ["src/app.py"], "main", "") is None


def test_resolve_route_combined_constraints_all_must_match() -> None:
    pf = _pf((RouteRule(policy="deep", paths=("**",), base_branch="staging-*"),))
    assert resolve_route(pf, ["x.py"], "staging-1", "") == "deep"
    assert resolve_route(pf, ["x.py"], "main", "") is None


# ---------------------------------------------------------------------------
# load_policy_file (git integration)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # `git show origin/{ref}:path` requires a remote named 'origin' with the
    # ref present, matching how actions/checkout sets up the workspace.
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    return repo


def test_load_policy_file_missing_returns_none_silently(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = load_policy_file(str(git_repo), "main")
    assert result is None
    assert capsys.readouterr().err == ""


def test_load_policy_file_valid(git_repo: Path) -> None:
    policy_dir = git_repo / ".github" / "ai-pr-review"
    policy_dir.mkdir(parents=True)
    (policy_dir / "policy.yml").write_text(
        "version: 1\n"
        "policies:\n"
        "  content:\n"
        "    agents: []\n"
        "routes:\n"
        "  - when: {paths: ['docs/**']}\n"
        "    policy: content\n"
        "default: content\n"
    )
    _git("add", ".", cwd=git_repo)
    _git("commit", "-q", "-m", "add policy", cwd=git_repo)
    _git("push", "-q", "origin", "main", cwd=git_repo)

    result = load_policy_file(str(git_repo), "main")
    assert result is not None
    assert result.default == "content"
    resolved = resolve_policy(result, "content")
    assert resolved.agents == ()


def test_load_policy_file_malformed_yaml_warns_and_returns_none(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_dir = git_repo / ".github" / "ai-pr-review"
    policy_dir.mkdir(parents=True)
    (policy_dir / "policy.yml").write_text("not: valid: yaml: [")
    _git("add", ".", cwd=git_repo)
    _git("commit", "-q", "-m", "bad policy", cwd=git_repo)
    _git("push", "-q", "origin", "main", cwd=git_repo)

    result = load_policy_file(str(git_repo), "main")
    assert result is None
    assert "WARNING" in capsys.readouterr().err


def test_load_policy_file_no_base_ref_returns_none() -> None:
    assert load_policy_file(".", "") is None


def test_load_policy_file_never_reads_from_pr_head_working_tree(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A policy.yml written only to the working tree (never committed/pushed
    to origin) must be invisible — this is the core trust-model guarantee:
    a PR cannot weaken its own review by editing its own policy file.
    """
    policy_dir = git_repo / ".github" / "ai-pr-review"
    policy_dir.mkdir(parents=True)
    (policy_dir / "policy.yml").write_text(
        "policies:\n  none: {agents: []}\nroutes: []\ndefault: none\n"
    )
    # Deliberately NOT committed/pushed — simulates a PR-head-only edit.
    result = load_policy_file(str(git_repo), "main")
    assert result is None


# ---------------------------------------------------------------------------
# examples/policy.yml.example — the maintainer-facing copy/paste template
# must stay parseable and resolve routes the way its own comments claim.
# ---------------------------------------------------------------------------


def _find_repo_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "examples" / "policy.yml.example").is_file():
            return candidate
    pytest.fail("Could not locate examples/policy.yml.example from repo root")


def test_example_policy_file_parses() -> None:
    import yaml

    path = _find_repo_root() / "examples" / "policy.yml.example"
    raw = yaml.safe_load(path.read_text())
    pf = _parse_policy_file(raw)
    assert set(pf.policies) == {"content", "feature", "integration", "deep"}
    assert pf.default == "feature"


def test_example_policy_file_resolves_documented_policies() -> None:
    import yaml

    path = _find_repo_root() / "examples" / "policy.yml.example"
    pf = _parse_policy_file(yaml.safe_load(path.read_text()))

    assert resolve_policy(pf, "content").review_mode == "quick"
    assert resolve_policy(pf, "content").agents == ()
    assert resolve_policy(pf, "feature").review_mode == "quick"
    assert resolve_policy(pf, "integration").agents == (
        "code-reviewer", "silent-failure-hunter", "edge-case-hunter",
    )
    assert resolve_policy(pf, "deep").review_mode == "full"


def test_example_policy_file_routes_match_as_documented() -> None:
    import yaml

    path = _find_repo_root() / "examples" / "policy.yml.example"
    pf = _parse_policy_file(yaml.safe_load(path.read_text()))

    assert resolve_route(pf, ["docs/index.md"], "main", "feature/x") == "content"
    assert resolve_route(pf, ["src/app.py"], "staging-1.2", "feature/x") == "integration"
    assert resolve_route(pf, ["src/app.py"], "main", "release/2.0") == "deep"
    assert resolve_route(pf, ["src/app.py"], "main", "feature/x") == "feature"
    assert resolve_route(pf, ["src/app.py"], "main", "random-branch") == "feature"  # default
