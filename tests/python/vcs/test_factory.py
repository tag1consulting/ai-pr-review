"""Tests for ai_pr_review.vcs.provider_from_env."""

from __future__ import annotations

import pytest

from ai_pr_review.vcs import (
    BitbucketProvider,
    GitHubProvider,
    GitLabProvider,
    ProviderConfigError,
    provider_from_env,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_provider_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [
        "VCS_PROVIDER",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
        "GITHUB_API_URL",
        "PR_NUMBER",
        "GITLAB_TOKEN",
        "CI_JOB_TOKEN",
        "GITLAB_PROJECT_ID",
        "CI_PROJECT_ID",
        "CI_PROJECT_PATH",
        "MR_IID",
        "GITLAB_DIFF_BASE_SHA",
        "CI_MERGE_REQUEST_DIFF_BASE_SHA",
        "GITLAB_API_URL",
        "GITLAB_BOT_USERNAME",
        "BITBUCKET_EMAIL",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_WORKSPACE",
        "BITBUCKET_REPO_SLUG",
    ]:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_default_provider_is_github(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    prov = provider_from_env()
    assert isinstance(prov, GitHubProvider)


def test_explicit_github(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "GitHub")  # case-insensitive
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "5")
    assert isinstance(provider_from_env(), GitHubProvider)


def test_gitlab(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-x")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "42")
    monkeypatch.setenv("MR_IID", "7")
    monkeypatch.setenv("GITLAB_DIFF_BASE_SHA", "abc1234")
    assert isinstance(provider_from_env(), GitLabProvider)


def test_gitlab_uses_ci_job_token_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("CI_JOB_TOKEN", "glcbt-x")
    monkeypatch.setenv("CI_PROJECT_PATH", "group/proj")
    monkeypatch.setenv("MR_IID", "1")
    monkeypatch.setenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", "abc1234")
    assert isinstance(provider_from_env(), GitLabProvider)


def test_bitbucket(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "bitbucket")
    monkeypatch.setenv("BITBUCKET_EMAIL", "x@y")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("BITBUCKET_WORKSPACE", "ws")
    monkeypatch.setenv("BITBUCKET_REPO_SLUG", "repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    assert isinstance(provider_from_env(), BitbucketProvider)


def test_bitbucket_falls_back_to_github_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "bitbucket")
    monkeypatch.setenv("BITBUCKET_EMAIL", "x@y")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ws/repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    assert isinstance(provider_from_env(), BitbucketProvider)


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "azure-devops")
    with pytest.raises(ProviderConfigError, match="Unknown VCS_PROVIDER"):
        provider_from_env()


# ---------------------------------------------------------------------------
# Missing env errors
# ---------------------------------------------------------------------------


def test_github_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "github")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    with pytest.raises(ProviderConfigError, match="GH_TOKEN"):
        provider_from_env()


def test_github_invalid_repository_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "no-slash")
    monkeypatch.setenv("PR_NUMBER", "1")
    with pytest.raises(ProviderConfigError, match="owner/repo"):
        provider_from_env()


def test_pr_number_must_be_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "not-a-number")
    with pytest.raises(ProviderConfigError, match="must be an integer"):
        provider_from_env()


def test_gitlab_missing_diff_base_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_TOKEN", "x")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "1")
    monkeypatch.setenv("MR_IID", "1")
    with pytest.raises(ProviderConfigError, match="GITLAB_DIFF_BASE_SHA"):
        provider_from_env()


def test_bitbucket_invalid_repo_format(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "bitbucket")
    monkeypatch.setenv("BITBUCKET_EMAIL", "x@y")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ws/proj/extra")  # too many slashes
    monkeypatch.setenv("PR_NUMBER", "1")
    with pytest.raises(ProviderConfigError, match="workspace/repo_slug"):
        provider_from_env()


# ---------------------------------------------------------------------------
# Whitespace stripping (#600)
# ---------------------------------------------------------------------------


def test_github_token_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "\ttok\n")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    prov = provider_from_env()
    assert isinstance(prov, GitHubProvider)
    assert prov.config.token == "tok"


def test_github_token_whitespace_only_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "   ")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    with pytest.raises(ProviderConfigError, match="GH_TOKEN"):
        provider_from_env()


def test_gitlab_token_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_TOKEN", "  tok  ")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "1")
    monkeypatch.setenv("MR_IID", "1")
    monkeypatch.setenv("GITLAB_DIFF_BASE_SHA", "abc123")
    prov = provider_from_env()
    assert isinstance(prov, GitLabProvider)
    assert prov.config.token == "tok"


def test_bitbucket_token_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "bitbucket")
    monkeypatch.setenv("BITBUCKET_EMAIL", " x@y\n")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "\ttok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ws/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    prov = provider_from_env()
    assert isinstance(prov, BitbucketProvider)
    assert prov.config.email == "x@y"
    assert prov.config.api_token == "tok"


def test_github_api_url_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("GITHUB_API_URL", "  https://ghe.example.com/api/v3\n")
    prov = provider_from_env()
    assert isinstance(prov, GitHubProvider)
    assert prov.config.base_url == "https://ghe.example.com/api/v3"


def test_github_api_url_whitespace_only_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("GITHUB_API_URL", "   ")
    prov = provider_from_env()
    assert isinstance(prov, GitHubProvider)
    assert prov.config.base_url == "https://api.github.com"


def test_gitlab_api_url_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "1")
    monkeypatch.setenv("MR_IID", "1")
    monkeypatch.setenv("GITLAB_DIFF_BASE_SHA", "abc123")
    monkeypatch.setenv("GITLAB_API_URL", "  https://gitlab.example.com/api/v4\t")
    prov = provider_from_env()
    assert isinstance(prov, GitLabProvider)
    assert prov.config.base_url == "https://gitlab.example.com/api/v4"


def test_gitlab_api_url_whitespace_only_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "1")
    monkeypatch.setenv("MR_IID", "1")
    monkeypatch.setenv("GITLAB_DIFF_BASE_SHA", "abc123")
    monkeypatch.setenv("GITLAB_API_URL", "   ")
    prov = provider_from_env()
    assert isinstance(prov, GitLabProvider)
    assert prov.config.base_url == "https://gitlab.com/api/v4"


def test_gitlab_project_id_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "gitlab")
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.setenv("GITLAB_PROJECT_ID", "\t123\n")
    monkeypatch.setenv("MR_IID", "1")
    monkeypatch.setenv("GITLAB_DIFF_BASE_SHA", "abc123")
    prov = provider_from_env()
    assert isinstance(prov, GitLabProvider)
    assert prov.config.project_id_or_path == "123"


def test_bitbucket_workspace_and_repo_slug_whitespace_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_provider_envs(monkeypatch)
    monkeypatch.setenv("VCS_PROVIDER", "bitbucket")
    monkeypatch.setenv("BITBUCKET_EMAIL", "x@y")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "tok")
    monkeypatch.setenv("BITBUCKET_WORKSPACE", "\tws\n")
    monkeypatch.setenv("BITBUCKET_REPO_SLUG", "  repo  ")
    monkeypatch.setenv("PR_NUMBER", "1")
    prov = provider_from_env()
    assert isinstance(prov, BitbucketProvider)
    assert prov.config.workspace == "ws"
    assert prov.config.repo_slug == "repo"
