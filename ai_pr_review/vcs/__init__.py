"""VCS provider factory + Protocol re-exports.

Call `provider_from_env()` to construct the right `VcsProvider` for the
current environment based on the `VCS_PROVIDER` env var. Each provider has
its own required env vars (token, repo identifier, PR/MR number) — see the
per-provider docstrings.
"""

from __future__ import annotations

import os

from ai_pr_review.vcs.bitbucket import (
    BitbucketConfig,
    BitbucketProvider,
)
from ai_pr_review.vcs.bitbucket import (
    build_client as build_bitbucket_client,
)
from ai_pr_review.vcs.github import (
    GitHubConfig,
    GitHubProvider,
)
from ai_pr_review.vcs.github import (
    build_client as build_github_client,
)
from ai_pr_review.vcs.gitlab import (
    GitLabConfig,
    GitLabProvider,
)
from ai_pr_review.vcs.gitlab import (
    build_client as build_gitlab_client,
)
from ai_pr_review.vcs.protocol import (
    DiffContext,
    FindingsResult,
    PostEvent,
    StaleResult,
    SummaryResult,
    VcsProvider,
)

__all__ = [
    "BitbucketConfig",
    "BitbucketProvider",
    "DiffContext",
    "FindingsResult",
    "GitHubConfig",
    "GitHubProvider",
    "GitLabConfig",
    "GitLabProvider",
    "PostEvent",
    "ProviderConfigError",
    "StaleResult",
    "SummaryResult",
    "VcsProvider",
    "provider_from_env",
]


class ProviderConfigError(ValueError):
    """Raised when env vars required by the selected provider are missing."""


def provider_from_env() -> VcsProvider:
    """Build the VcsProvider implied by `VCS_PROVIDER` and per-provider envs."""
    name = (os.environ.get("VCS_PROVIDER") or "github").strip().lower()
    if name == "github":
        return _build_github_from_env()
    if name == "gitlab":
        return _build_gitlab_from_env()
    if name == "bitbucket":
        return _build_bitbucket_from_env()
    raise ProviderConfigError(
        f"Unknown VCS_PROVIDER {name!r}; expected one of github/gitlab/bitbucket"
    )


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ProviderConfigError(f"{name} is required for the selected VCS provider")
    return val


def _require_int_env(name: str) -> int:
    raw = _require_env(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ProviderConfigError(
            f"{name} must be an integer (got {raw!r})"
        ) from exc


def _build_github_from_env() -> GitHubProvider:
    """Build GitHubProvider from env.

    Required: GH_TOKEN (or GITHUB_TOKEN), GITHUB_REPOSITORY (owner/repo), PR_NUMBER.
    Optional: GITHUB_API_URL (defaults to https://api.github.com).
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ProviderConfigError(
            "GH_TOKEN (or GITHUB_TOKEN) is required for VCS_PROVIDER=github"
        )
    repo = _require_env("GITHUB_REPOSITORY")
    if "/" not in repo:
        raise ProviderConfigError(
            f"GITHUB_REPOSITORY must be 'owner/repo' (got {repo!r})"
        )
    owner, name = repo.split("/", 1)
    pr_number = _require_int_env("PR_NUMBER")
    base_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"

    config = GitHubConfig(
        owner=owner, repo=name, pr_number=pr_number, token=token, base_url=base_url
    )
    return GitHubProvider(config=config, client=build_github_client(config))


def _build_gitlab_from_env() -> GitLabProvider:
    """Build GitLabProvider from env.

    Required: GITLAB_TOKEN (or CI_JOB_TOKEN), MR_IID, GITLAB_DIFF_BASE_SHA
              (or CI_MERGE_REQUEST_DIFF_BASE_SHA).
    Project: GITLAB_PROJECT_ID, CI_PROJECT_ID, CI_PROJECT_PATH, or
             GITHUB_REPOSITORY (in priority order).
    Optional: GITLAB_API_URL, GITLAB_BOT_USERNAME.
    """
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN")
    if not token:
        raise ProviderConfigError(
            "GITLAB_TOKEN (or CI_JOB_TOKEN) is required for VCS_PROVIDER=gitlab"
        )
    project = (
        os.environ.get("GITLAB_PROJECT_ID")
        or os.environ.get("CI_PROJECT_ID")
        or os.environ.get("CI_PROJECT_PATH")
        or os.environ.get("GITHUB_REPOSITORY")
    )
    if not project:
        raise ProviderConfigError(
            "Cannot resolve GitLab project; set one of GITLAB_PROJECT_ID, "
            "CI_PROJECT_ID, CI_PROJECT_PATH, or GITHUB_REPOSITORY"
        )
    mr_iid = _require_int_env("MR_IID")
    diff_base_sha = (
        os.environ.get("GITLAB_DIFF_BASE_SHA")
        or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    )
    if not diff_base_sha:
        raise ProviderConfigError(
            "GITLAB_DIFF_BASE_SHA (or CI_MERGE_REQUEST_DIFF_BASE_SHA) is required"
        )
    base_url = os.environ.get("GITLAB_API_URL") or "https://gitlab.com/api/v4"
    # Normalize: if the caller passed the host without /api/v4 (e.g. the
    # action.yml default "https://gitlab.com"), append the path so httpx
    # constructs correct absolute URLs instead of silently returning empty bodies.
    if base_url.rstrip("/").endswith("/api/v4"):
        pass  # already correct
    elif "/api/" not in base_url:
        base_url = base_url.rstrip("/") + "/api/v4"
    bot_username = os.environ.get("GITLAB_BOT_USERNAME") or None

    config = GitLabConfig(
        project_id_or_path=project,
        mr_iid=mr_iid,
        token=token,
        diff_base_sha=diff_base_sha,
        bot_username=bot_username,
        base_url=base_url,
    )
    return GitLabProvider(config=config, client=build_gitlab_client(config))


def _build_bitbucket_from_env() -> BitbucketProvider:
    """Build BitbucketProvider from env.

    Required: BITBUCKET_EMAIL, BITBUCKET_API_TOKEN, PR_NUMBER.
    Repo: BITBUCKET_WORKSPACE + BITBUCKET_REPO_SLUG, or GITHUB_REPOSITORY
          (in 'workspace/repo_slug' form).
    """
    email = _require_env("BITBUCKET_EMAIL")
    token = _require_env("BITBUCKET_API_TOKEN")
    workspace = os.environ.get("BITBUCKET_WORKSPACE") or ""
    repo_slug = os.environ.get("BITBUCKET_REPO_SLUG") or ""
    if not (workspace and repo_slug):
        repo_env = os.environ.get("GITHUB_REPOSITORY") or ""
        if repo_env.count("/") != 1:
            raise ProviderConfigError(
                "Set BITBUCKET_WORKSPACE + BITBUCKET_REPO_SLUG, or "
                "GITHUB_REPOSITORY in 'workspace/repo_slug' form"
            )
        workspace, repo_slug = repo_env.split("/", 1)
    pr_id = _require_int_env("PR_NUMBER")

    config = BitbucketConfig(
        workspace=workspace,
        repo_slug=repo_slug,
        pr_id=pr_id,
        email=email,
        api_token=token,
    )
    return BitbucketProvider(config=config, client=build_bitbucket_client(config))
