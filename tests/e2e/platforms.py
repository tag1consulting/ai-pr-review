"""Per-platform adapters: create a run commit, open a PR/MR, fetch posted
evidence, and clean up -- via each platform's REST/GraphQL content API only.

Deliberately does NOT import anything from ai_pr_review.vcs. That package is
what posts the review content this harness is verifying; if a bug in its
marker/formatting logic were shared with the verifier, the harness could
pass while the real behavior is wrong. Keeping this module's HTTP calls
independent (raw httpx against each provider's public API) is a structural
guard against that, not an oversight.

No local git clone/push anywhere: every commit is created via the provider's
"create/update file" content API on top of the pinned seed SHA, so a run
needs no local git credentials or working tree.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import PINNED_SEED_SHA, PLATFORMS, RUN_MARKER_PATH, PlatformConfig

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_MAX_RETRIES = 4
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def mask(text: str) -> str:
    """Redact anything that looks like a bearer token / password / Basic
    auth header from a string that might end up in a log line or exception
    message. Defense in depth on top of "never log token values directly".
    """
    text = re.sub(r"(Bearer|Basic|token)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 <redacted>", text, flags=re.I)
    # GitLab's auth header is "PRIVATE-TOKEN: <value>" -- no Bearer/Basic/token
    # keyword, so the pattern above never matches it (e.g. in a verbose httpx
    # exception repr that includes request headers).
    text = re.sub(r"(PRIVATE-TOKEN)\s*:?\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 <redacted>", text, flags=re.I)
    text = re.sub(r"(https?://)[^:@/\s]+:[^@/\s]+@", r"\1<redacted>@", text)
    return text


class AdapterError(RuntimeError):
    """Raised by an adapter on an unrecoverable API failure. Always carries
    a mask()-ed message -- construct via `AdapterError(mask(str(exc)))`."""


@dataclass(frozen=True)
class RunCommit:
    platform: str
    run_id: str
    branch: str
    commit_sha: str


@dataclass(frozen=True)
class PullRequest:
    platform: str
    number: int
    url: str
    branch: str
    run_commit: RunCommit


@dataclass
class RawEvidence:
    """Everything fetched back from a platform about one run's PR/MR."""

    summary_body: str = ""
    summary_hidden_markers: list[str] = field(default_factory=list)
    inline_comments: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)  # Bitbucket only
    truncated: bool = False


def _sleep_for_retry(attempt: int, response: httpx.Response | None) -> None:
    retry_after = None
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
    delay = retry_after if retry_after is not None else min(2**attempt, 30)
    time.sleep(delay)


def _request_with_retry(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES - 1:
                raise AdapterError(mask(f"{method} {url} failed after retries: {exc!r}")) from exc
            _sleep_for_retry(attempt, None)
            continue

        # GitHub secondary rate limit surfaces as a 403 with this marker in
        # the body rather than a 429 -- treat it the same as a retryable
        # rate limit rather than a hard auth failure.
        is_secondary_rate_limit = (
            response.status_code == 403
            and "secondary rate limit" in response.text.lower()
        )
        if response.status_code in _RETRYABLE_STATUS or is_secondary_rate_limit:
            if attempt == _MAX_RETRIES - 1:
                raise AdapterError(
                    mask(f"{method} {url} returned {response.status_code} after retries: {response.text[:500]}")
                )
            _sleep_for_retry(attempt, response)
            continue
        return response
    raise AdapterError(mask(f"{method} {url} failed: {last_exc!r}"))


class PlatformAdapter(Protocol):
    """Shared contract every per-platform adapter implements."""

    config: PlatformConfig

    def preflight(self) -> None:
        """Verify token + repo access. Must not write anything."""
        ...

    def create_run_commit(self, run_id: str) -> RunCommit:
        """Create ONE commit on top of the pinned seed SHA adding
        RUN_MARKER_PATH containing run_id, via the content API (no local
        git)."""
        ...

    def open_pr(self, run_commit: RunCommit) -> PullRequest:
        ...

    def fetch_summary(self, pr: PullRequest) -> RawEvidence:
        """Fetch the posted summary. Must be fully paginated."""
        ...

    def fetch_inline(self, pr: PullRequest, evidence: RawEvidence) -> None:
        """Populate evidence.inline_comments in place (fully paginated)."""
        ...

    def fetch_annotations(self, pr: PullRequest, evidence: RawEvidence) -> None:
        """Bitbucket only. No-op for other platforms."""
        ...

    def close(self, pr: PullRequest) -> None:
        ...

    def delete_branch(self, branch: str) -> None:
        ...


class _BaseAdapter:
    """Shared __init__ and a default no-op fetch_annotations for adapters
    that satisfy the PlatformAdapter protocol structurally (not via
    inheritance -- Protocol is duck-typed)."""

    def __init__(self, config: PlatformConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def fetch_annotations(self, pr: PullRequest, evidence: RawEvidence) -> None:
        """Default no-op; only Bitbucket overrides this."""
        return


class GitHubAdapter(_BaseAdapter):
    """GitHub REST v3 (content API + issues/pulls comments) via a PAT."""

    API_ROOT = "https://api.github.com"

    def __init__(self, config: PlatformConfig, seeder_token: str, reviewer_token: str = "",
                 client: httpx.Client | None = None) -> None:
        super().__init__(config, client)
        self._seeder_token = seeder_token
        self._reviewer_token = reviewer_token or seeder_token

    def _headers(self, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token or self._seeder_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def preflight(self) -> None:
        url = f"{self.API_ROOT}/repos/{self.config.repo_slug}"
        resp = _request_with_retry(self._client, "GET", url, headers=self._headers())
        if resp.status_code != 200:
            raise AdapterError(mask(f"GitHub preflight failed for {self.config.repo_slug}: "
                                     f"{resp.status_code} {resp.text[:300]}"))

    def create_run_commit(self, run_id: str) -> RunCommit:
        branch = f"e2e/{run_id}"
        # Create the branch ref pointing at the pinned seed SHA.
        ref_url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/git/refs"
        resp = _request_with_retry(
            self._client, "POST", ref_url, headers=self._headers(),
            json={"ref": f"refs/heads/{branch}", "sha": PINNED_SEED_SHA},
        )
        if resp.status_code not in (201,):
            raise AdapterError(mask(f"GitHub create-ref failed: {resp.status_code} {resp.text[:300]}"))

        content_url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/contents/{RUN_MARKER_PATH}"
        payload = {
            "message": f"e2e: run {run_id}",
            "content": base64.b64encode(run_id.encode()).decode(),
            "branch": branch,
        }
        try:
            put_resp = _request_with_retry(self._client, "PUT", content_url, headers=self._headers(), json=payload)
            if put_resp.status_code not in (200, 201):
                raise AdapterError(mask(f"GitHub create-file failed: {put_resp.status_code} {put_resp.text[:300]}"))
        except AdapterError:
            # The branch was already created above; a failure here would
            # otherwise leave an orphan branch (never tracked by
            # _opened_prs, so never cleaned up) littering the shared test
            # repo with zero debugging value.
            self._delete_branch_best_effort(branch)
            raise
        commit_sha = put_resp.json()["commit"]["sha"]
        return RunCommit(platform="github", run_id=run_id, branch=branch, commit_sha=commit_sha)

    def _delete_branch_best_effort(self, branch: str) -> None:
        try:
            self.delete_branch(branch)
        except AdapterError as exc:
            logger.warning("GitHub: could not clean up orphan branch %s: %s", branch, mask(str(exc)))

    def open_pr(self, run_commit: RunCommit) -> PullRequest:
        url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/pulls"
        resp = _request_with_retry(
            self._client, "POST", url, headers=self._headers(),
            json={
                "title": f"e2e: {run_commit.run_id}",
                "head": run_commit.branch,
                "base": self.config.base_ref,
                "body": f"Automated e2e harness run `{run_commit.run_id}`.",
            },
        )
        if resp.status_code not in (201,):
            # The branch+commit already exist; clean up the orphan before
            # raising, same reasoning as create_run_commit above.
            self._delete_branch_best_effort(run_commit.branch)
            raise AdapterError(mask(f"GitHub open-pr failed: {resp.status_code} {resp.text[:300]}"))
        data = resp.json()
        return PullRequest(platform="github", number=data["number"], url=data["html_url"],
                            branch=run_commit.branch, run_commit=run_commit)

    def fetch_summary(self, pr: PullRequest) -> RawEvidence:
        evidence = RawEvidence()
        url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/issues/{pr.number}/comments"
        page = 1
        comments: list[dict[str, Any]] = []
        while True:
            resp = _request_with_retry(
                self._client, "GET", url, headers=self._headers(),
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                raise AdapterError(mask(f"GitHub fetch-summary failed: {resp.status_code} {resp.text[:300]}"))
            batch = resp.json()
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 20:  # sanity bound; a real PR should never need this many pages
                evidence.truncated = True
                break
        # Also check review bodies (the bot may post as a review, not a plain comment).
        reviews_url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/pulls/{pr.number}/reviews"
        rresp = _request_with_retry(self._client, "GET", reviews_url, headers=self._headers())
        if rresp.status_code != 200:
            # Must raise, not silently treat as "no reviews exist": an
            # auth/permission failure here would otherwise surface downstream
            # as a false "no marker found" product_failure instead of the
            # infra_failure it actually is.
            raise AdapterError(mask(f"GitHub fetch-reviews failed: {rresp.status_code} {rresp.text[:300]}"))
        reviews = rresp.json()
        bodies = [c.get("body", "") for c in comments if c.get("body")] + \
                 [r.get("body", "") for r in reviews if r.get("body")]
        evidence.summary_body = "\n\n---\n\n".join(bodies)
        return evidence

    def fetch_inline(self, pr: PullRequest, evidence: RawEvidence) -> None:
        url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/pulls/{pr.number}/comments"
        page = 1
        while True:
            resp = _request_with_retry(
                self._client, "GET", url, headers=self._headers(),
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                raise AdapterError(mask(f"GitHub fetch-inline failed: {resp.status_code} {resp.text[:300]}"))
            batch = resp.json()
            evidence.inline_comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 20:
                evidence.truncated = True
                break

    def close(self, pr: PullRequest) -> None:
        url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/pulls/{pr.number}"
        resp = _request_with_retry(self._client, "PATCH", url, headers=self._headers(), json={"state": "closed"})
        if resp.status_code != 200:
            raise AdapterError(mask(f"GitHub close-pr failed: {resp.status_code} {resp.text[:300]}"))

    def delete_branch(self, branch: str) -> None:
        url = f"{self.API_ROOT}/repos/{self.config.repo_slug}/git/refs/heads/{branch}"
        resp = _request_with_retry(self._client, "DELETE", url, headers=self._headers())
        if resp.status_code not in (204, 422):  # 422: already gone
            raise AdapterError(mask(f"GitHub delete-branch failed: {resp.status_code} {resp.text[:300]}"))


class GitLabAdapter(_BaseAdapter):
    """GitLab REST v4 (commits/files content API + merge requests)."""

    def __init__(self, config: PlatformConfig, token: str, api_url: str = "https://gitlab.com",
                 client: httpx.Client | None = None) -> None:
        super().__init__(config, client)
        self._token = token
        self._api_url = api_url.rstrip("/")
        from urllib.parse import quote
        self._project_path = quote(config.repo_slug, safe="")

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token}

    def preflight(self) -> None:
        url = f"{self._api_url}/api/v4/projects/{self._project_path}"
        resp = _request_with_retry(self._client, "GET", url, headers=self._headers())
        if resp.status_code != 200:
            raise AdapterError(mask(f"GitLab preflight failed: {resp.status_code} {resp.text[:300]}"))

    def create_run_commit(self, run_id: str) -> RunCommit:
        branch = f"e2e/{run_id}"
        branches_url = f"{self._api_url}/api/v4/projects/{self._project_path}/repository/branches"
        resp = _request_with_retry(
            self._client, "POST", branches_url, headers=self._headers(),
            params={"branch": branch, "ref": PINNED_SEED_SHA},
        )
        if resp.status_code not in (201,):
            raise AdapterError(mask(f"GitLab create-branch failed: {resp.status_code} {resp.text[:300]}"))

        commits_url = f"{self._api_url}/api/v4/projects/{self._project_path}/repository/commits"
        payload = {
            "branch": branch,
            "commit_message": f"e2e: run {run_id}",
            "actions": [{"action": "create", "file_path": RUN_MARKER_PATH, "content": run_id}],
        }
        try:
            cresp = _request_with_retry(self._client, "POST", commits_url, headers=self._headers(), json=payload)
            if cresp.status_code not in (201,):
                raise AdapterError(mask(f"GitLab create-commit failed: {cresp.status_code} {cresp.text[:300]}"))
        except AdapterError:
            # Same orphan-branch reasoning as GitHubAdapter.create_run_commit.
            self._delete_branch_best_effort(branch)
            raise
        commit_sha = cresp.json()["id"]
        return RunCommit(platform="gitlab", run_id=run_id, branch=branch, commit_sha=commit_sha)

    def _delete_branch_best_effort(self, branch: str) -> None:
        try:
            self.delete_branch(branch)
        except AdapterError as exc:
            logger.warning("GitLab: could not clean up orphan branch %s: %s", branch, mask(str(exc)))

    def open_pr(self, run_commit: RunCommit) -> PullRequest:
        url = f"{self._api_url}/api/v4/projects/{self._project_path}/merge_requests"
        resp = _request_with_retry(
            self._client, "POST", url, headers=self._headers(),
            json={
                "source_branch": run_commit.branch,
                "target_branch": self.config.base_ref,
                "title": f"e2e: {run_commit.run_id}",
                "description": f"Automated e2e harness run `{run_commit.run_id}`.",
            },
        )
        if resp.status_code not in (201,):
            self._delete_branch_best_effort(run_commit.branch)
            raise AdapterError(mask(f"GitLab open-mr failed: {resp.status_code} {resp.text[:300]}"))
        data = resp.json()
        return PullRequest(platform="gitlab", number=data["iid"], url=data["web_url"],
                            branch=run_commit.branch, run_commit=run_commit)

    def fetch_summary(self, pr: PullRequest) -> RawEvidence:
        evidence = RawEvidence()
        url = f"{self._api_url}/api/v4/projects/{self._project_path}/merge_requests/{pr.number}/notes"
        page = 1
        notes: list[dict[str, Any]] = []
        while True:
            resp = _request_with_retry(
                self._client, "GET", url, headers=self._headers(),
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                raise AdapterError(mask(f"GitLab fetch-summary failed: {resp.status_code} {resp.text[:300]}"))
            batch = resp.json()
            notes.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 20:
                evidence.truncated = True
                break
        evidence.summary_body = "\n\n---\n\n".join(n.get("body", "") for n in notes if n.get("body"))
        return evidence

    def fetch_inline(self, pr: PullRequest, evidence: RawEvidence) -> None:
        url = f"{self._api_url}/api/v4/projects/{self._project_path}/merge_requests/{pr.number}/discussions"
        page = 1
        while True:
            resp = _request_with_retry(
                self._client, "GET", url, headers=self._headers(),
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                raise AdapterError(mask(f"GitLab fetch-inline failed: {resp.status_code} {resp.text[:300]}"))
            batch = resp.json()
            for discussion in batch:
                evidence.inline_comments.extend(discussion.get("notes", []))
            if len(batch) < 100:
                break
            page += 1
            if page > 20:
                evidence.truncated = True
                break

    def close(self, pr: PullRequest) -> None:
        url = f"{self._api_url}/api/v4/projects/{self._project_path}/merge_requests/{pr.number}"
        resp = _request_with_retry(self._client, "PUT", url, headers=self._headers(), json={"state_event": "close"})
        if resp.status_code != 200:
            raise AdapterError(mask(f"GitLab close-mr failed: {resp.status_code} {resp.text[:300]}"))

    def delete_branch(self, branch: str) -> None:
        from urllib.parse import quote
        url = f"{self._api_url}/api/v4/projects/{self._project_path}/repository/branches/{quote(branch, safe='')}"
        resp = _request_with_retry(self._client, "DELETE", url, headers=self._headers())
        if resp.status_code not in (204, 404):
            raise AdapterError(mask(f"GitLab delete-branch failed: {resp.status_code} {resp.text[:300]}"))


class BitbucketAdapter(_BaseAdapter):
    """Bitbucket Cloud REST 2.0 (src content API + pullrequests + Code Insights)."""

    API_ROOT = "https://api.bitbucket.org/2.0"

    def __init__(self, config: PlatformConfig, email: str, api_token: str,
                 client: httpx.Client | None = None) -> None:
        super().__init__(config, client)
        self._auth = (email, api_token)

    def preflight(self) -> None:
        url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}"
        resp = _request_with_retry(self._client, "GET", url, auth=self._auth)
        if resp.status_code != 200:
            raise AdapterError(mask(f"Bitbucket preflight failed: {resp.status_code} {resp.text[:300]}"))

    def create_run_commit(self, run_id: str) -> RunCommit:
        branch = f"e2e/{run_id}"
        # Bitbucket's src endpoint creates a commit on a new branch in one
        # multipart POST: branch + file content, parented on the pinned SHA.
        url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/src"
        resp = _request_with_retry(
            self._client, "POST", url, auth=self._auth,
            data={
                "branch": branch,
                "parents": PINNED_SEED_SHA,
                "message": f"e2e: run {run_id}",
            },
            files={RUN_MARKER_PATH: run_id.encode()},
        )
        if resp.status_code not in (201,):
            raise AdapterError(mask(f"Bitbucket create-commit failed: {resp.status_code} {resp.text[:300]}"))

        # The src POST doesn't return the new commit SHA directly; resolve
        # it from the branch we just created.
        branch_url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/refs/branches/{branch}"
        bresp = _request_with_retry(self._client, "GET", branch_url, auth=self._auth)
        if bresp.status_code != 200:
            raise AdapterError(mask(f"Bitbucket resolve-branch-sha failed: {bresp.status_code} {bresp.text[:300]}"))
        commit_sha = bresp.json()["target"]["hash"]
        return RunCommit(platform="bitbucket", run_id=run_id, branch=branch, commit_sha=commit_sha)

    def open_pr(self, run_commit: RunCommit) -> PullRequest:
        url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/pullrequests"
        resp = _request_with_retry(
            self._client, "POST", url, auth=self._auth,
            json={
                "title": f"e2e: {run_commit.run_id}",
                "source": {"branch": {"name": run_commit.branch}},
                "destination": {"branch": {"name": self.config.base_ref}},
                "description": f"Automated e2e harness run `{run_commit.run_id}`.",
            },
        )
        if resp.status_code not in (201,):
            raise AdapterError(mask(f"Bitbucket open-pr failed: {resp.status_code} {resp.text[:300]}"))
        data = resp.json()
        return PullRequest(platform="bitbucket", number=data["id"], url=data["links"]["html"]["href"],
                            branch=run_commit.branch, run_commit=run_commit)

    def fetch_summary(self, pr: PullRequest) -> RawEvidence:
        evidence = RawEvidence()
        url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/pullrequests/{pr.number}/comments"
        comments: list[dict[str, Any]] = []
        page_url: str | None = url
        pages_seen = 0
        while page_url:
            resp = _request_with_retry(self._client, "GET", page_url, auth=self._auth)
            if resp.status_code != 200:
                raise AdapterError(mask(f"Bitbucket fetch-summary failed: {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            comments.extend(data.get("values", []))
            page_url = data.get("next")
            pages_seen += 1
            if pages_seen > 20:
                evidence.truncated = True
                break
        bodies = [c.get("content", {}).get("raw", "") for c in comments]
        evidence.summary_body = "\n\n---\n\n".join(b for b in bodies if b)
        return evidence

    def fetch_inline(self, pr: PullRequest, evidence: RawEvidence) -> None:
        # Bitbucket doesn't separate inline from top-level comments in the
        # API response (each comment carries an optional `inline` key) --
        # reuse the same fetched set and let verify.py filter on `inline`.
        if not evidence.inline_comments:
            url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/pullrequests/{pr.number}/comments"
            page_url: str | None = url
            pages_seen = 0
            while page_url:
                resp = _request_with_retry(self._client, "GET", page_url, auth=self._auth)
                if resp.status_code != 200:
                    raise AdapterError(mask(f"Bitbucket fetch-inline failed: {resp.status_code} {resp.text[:300]}"))
                data = resp.json()
                evidence.inline_comments.extend(
                    c for c in data.get("values", []) if c.get("inline")
                )
                page_url = data.get("next")
                pages_seen += 1
                if pages_seen > 20:
                    evidence.truncated = True
                    break

    def fetch_annotations(self, pr: PullRequest, evidence: RawEvidence) -> None:
        # Annotations are keyed by the run's OWN commit SHA (per-run
        # uniqueness), never a shared/stale one -- see run_commit.commit_sha.
        commit_sha = pr.run_commit.commit_sha
        report_url = (
            f"{self.API_ROOT}/repositories/{self.config.repo_slug}/commit/"
            f"{commit_sha}/reports/ai-pr-review"
        )
        resp = _request_with_retry(self._client, "GET", report_url, auth=self._auth)
        if resp.status_code == 404:
            return  # no Code Insights report for this commit; not an error
        if resp.status_code != 200:
            raise AdapterError(mask(f"Bitbucket fetch-report failed: {resp.status_code} {resp.text[:300]}"))
        ann_url = f"{report_url}/annotations"
        page_url: str | None = ann_url
        pages_seen = 0
        while page_url:
            aresp = _request_with_retry(self._client, "GET", page_url, auth=self._auth)
            if aresp.status_code != 200:
                raise AdapterError(mask(f"Bitbucket fetch-annotations failed: {aresp.status_code} {aresp.text[:300]}"))
            data = aresp.json()
            evidence.annotations.extend(data.get("values", []))
            page_url = data.get("next")
            pages_seen += 1
            if pages_seen > 20:
                evidence.truncated = True
                break

    def close(self, pr: PullRequest) -> None:
        url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/pullrequests/{pr.number}/decline"
        resp = _request_with_retry(self._client, "POST", url, auth=self._auth)
        if resp.status_code != 200:
            raise AdapterError(mask(f"Bitbucket decline-pr failed: {resp.status_code} {resp.text[:300]}"))

    def delete_branch(self, branch: str) -> None:
        from urllib.parse import quote
        url = f"{self.API_ROOT}/repositories/{self.config.repo_slug}/refs/branches/{quote(branch, safe='')}"
        resp = _request_with_retry(self._client, "DELETE", url, auth=self._auth)
        if resp.status_code not in (204, 404):
            raise AdapterError(mask(f"Bitbucket delete-branch failed: {resp.status_code} {resp.text[:300]}"))


def build_adapter(platform: str, env: dict[str, str]) -> PlatformAdapter:
    """Construct the adapter for *platform* from E2E_* env vars in *env*."""
    config = PLATFORMS[platform]
    if platform == "github":
        return GitHubAdapter(
            config,
            seeder_token=env.get("E2E_GITHUB_TOKEN", ""),
            reviewer_token=env.get("E2E_GITHUB_REVIEWER_TOKEN", ""),
        )
    if platform == "gitlab":
        return GitLabAdapter(config, token=env.get("E2E_GITLAB_TOKEN", ""))
    if platform == "bitbucket":
        return BitbucketAdapter(
            config,
            email=env.get("E2E_BITBUCKET_EMAIL", ""),
            api_token=env.get("E2E_BITBUCKET_TOKEN", ""),
        )
    raise ValueError(f"unknown platform {platform!r}")
