"""VCS provider factory + Protocol re-exports.

Call `provider_from_env()` to construct the right `VcsProvider` for the
current environment based on the `VCS_PROVIDER` env var. Each provider has
its own required env vars (token, repo identifier, PR/MR number) — see the
per-provider docstrings.
"""

from __future__ import annotations

import logging
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
from ai_pr_review.vcs.github import (
    build_elevated_client as build_github_elevated_client,
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

logger = logging.getLogger(__name__)

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
    val = (os.environ.get(name) or "").strip()
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
    Optional: GITHUB_API_URL (defaults to https://api.github.com), GITHUB_BOT_USERNAME
    (defaults to GitHubConfig's own "github-actions[bot]" default).

    Token split (#734): when BOTH `GITHUB_TOKEN` and `GH_TOKEN` are set and
    differ, `GITHUB_TOKEN` becomes the provider's primary token (used for
    every write except thread resolution) and `GH_TOKEN` becomes the
    elevated token reserved for `resolve_thread`/`unresolve_thread` --
    GitHub blocks the `resolveReviewThread` GraphQL mutation under the
    default Actions token from a comment-triggered workflow, so a PAT/App
    token is unavoidable for that one call. When only one of the two is
    set (today's single-token consumers), that token is used for
    everything, matching pre-#734 behavior exactly.
    """
    github_token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    gh_token = (os.environ.get("GH_TOKEN") or "").strip()
    token = github_token or gh_token
    if not token:
        raise ProviderConfigError(
            "GH_TOKEN (or GITHUB_TOKEN) is required for VCS_PROVIDER=github"
        )
    elevated_token = gh_token if gh_token and gh_token != token else None
    repo = _require_env("GITHUB_REPOSITORY")
    if "/" not in repo:
        raise ProviderConfigError(
            f"GITHUB_REPOSITORY must be 'owner/repo' (got {repo!r})"
        )
    owner, name = repo.split("/", 1)
    pr_number = _require_int_env("PR_NUMBER")
    base_url = (os.environ.get("GITHUB_API_URL") or "").strip() or "https://api.github.com"

    canonical_reuse = (
        os.environ.get("AI_CANONICAL_REUSE", "true").strip().lower()
        not in ("false", "0", "no")
    )
    # GITHUB_BOT_USERNAME mirrors the GitLab provider's existing
    # GITLAB_BOT_USERNAME override: it lets a consumer whose reviews post
    # under an identity other than the default GitHub Actions bot (a custom
    # GitHub App, or -- as in this project's own e2e test harness, which
    # authenticates with a personal access token -- a human account) tell
    # GitHubConfig which login actually owns its prior reviews/comments.
    # Without this, _list_prior_bot_reviews()/list_bot_reviews()'s REST-side
    # `review.user.login == bot_login` filter (and any GraphQL-side thread
    # ownership check keyed off the same config value) can never recognize
    # that identity's own prior output as "ours", so canonical-review
    # reuse's review-level PUT-in-place path never engages -- confirmed live
    # against the GitHub test PR: thread-level classification worked
    # correctly, but reused_review stayed False because no prior review was
    # ever recognized as a "bot review" to select as canonical. Only passed
    # through when set; otherwise GitHubConfig's own "github-actions[bot]"
    # default applies.
    bot_login = os.environ.get("GITHUB_BOT_USERNAME", "").strip()
    config = GitHubConfig(
        owner=owner, repo=name, pr_number=pr_number, token=token, base_url=base_url,
        canonical_reuse=canonical_reuse, elevated_token=elevated_token,
        **({"bot_login": bot_login} if bot_login else {}),
    )
    return GitHubProvider(
        config=config,
        client=build_github_client(config),
        elevated_client=build_github_elevated_client(config),
    )


def _build_gitlab_from_env() -> GitLabProvider:
    """Build GitLabProvider from env.

    Required: GITLAB_TOKEN (or CI_JOB_TOKEN), MR_IID, GITLAB_DIFF_BASE_SHA
              (or CI_MERGE_REQUEST_DIFF_BASE_SHA).
    Project: GITLAB_PROJECT_ID, CI_PROJECT_ID, CI_PROJECT_PATH, or
             GITHUB_REPOSITORY (in priority order).
    Optional: GITLAB_API_URL, GITLAB_BOT_USERNAME.
    """
    token = (os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN") or "").strip()
    if not token:
        raise ProviderConfigError(
            "GITLAB_TOKEN (or CI_JOB_TOKEN) is required for VCS_PROVIDER=gitlab"
        )
    project = (
        os.environ.get("GITLAB_PROJECT_ID")
        or os.environ.get("CI_PROJECT_ID")
        or os.environ.get("CI_PROJECT_PATH")
        or os.environ.get("GITHUB_REPOSITORY")
        or ""
    ).strip()
    if not project:
        raise ProviderConfigError(
            "Cannot resolve GitLab project; set one of GITLAB_PROJECT_ID, "
            "CI_PROJECT_ID, CI_PROJECT_PATH, or GITHUB_REPOSITORY"
        )
    mr_iid = _require_int_env("MR_IID")
    diff_base_sha = (
        os.environ.get("GITLAB_DIFF_BASE_SHA")
        or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
        or ""
    ).strip()
    if not diff_base_sha:
        raise ProviderConfigError(
            "GITLAB_DIFF_BASE_SHA (or CI_MERGE_REQUEST_DIFF_BASE_SHA) is required"
        )
    base_url = (os.environ.get("GITLAB_API_URL") or "").strip() or "https://gitlab.com/api/v4"
    # Normalize: if the caller passed the host without /api/v4 (e.g. the
    # action.yml default "https://gitlab.com"), append the path so httpx
    # constructs correct absolute URLs instead of silently returning empty bodies.
    if base_url.rstrip("/").endswith("/api/v4"):
        pass  # already correct
    elif "/api/" not in base_url:
        original = base_url
        base_url = base_url.rstrip("/") + "/api/v4"
        logger.warning(
            "GITLAB_API_URL %r did not include /api/v4; normalized to %r",
            original,
            base_url,
        )
    bot_username = os.environ.get("GITLAB_BOT_USERNAME") or None

    # Cross-run finding dedup (#710): a per-run "kept alive" set already
    # fixes an independent bug (resolve_stale immediately resolving a
    # discussion post_findings just created/kept, since GitLab's resolve_stale
    # has no equivalent of GitHub's _kept_alive_thread_ids) regardless of this
    # flag; the flag only gates the fuzzy-match "skip reposting an unchanged
    # finding" behavior itself. Mirrors AI_CANONICAL_REUSE's parse, but kept
    # as a separate flag since GitLab has no canonical review to couple it to.
    cross_run_dedup = (
        os.environ.get("AI_GITLAB_CROSS_RUN_DEDUP", "true").strip().lower()
        not in ("false", "0", "no")
    )

    config = GitLabConfig(
        project_id_or_path=project,
        mr_iid=mr_iid,
        token=token,
        diff_base_sha=diff_base_sha,
        bot_username=bot_username,
        base_url=base_url,
        cross_run_dedup=cross_run_dedup,
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
    workspace = (os.environ.get("BITBUCKET_WORKSPACE") or "").strip()
    repo_slug = (os.environ.get("BITBUCKET_REPO_SLUG") or "").strip()
    if not (workspace and repo_slug):
        repo_env = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
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
