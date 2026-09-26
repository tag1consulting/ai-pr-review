"""Deterministic e2e harness CLI.

Subcommands: plan (offline validation), preflight (auth/access check, no
writes, no LLM), run (the real thing -- opens throwaway PRs, runs the
container, verifies posting, and cleans up on pass only).

See tests/e2e/README.md for exit codes, required env vars, and the
checkpoint-before-live-run warning: `run` and `preflight` cost real API
money and must not be invoked without explicit human confirmation, per this
repo's CLAUDE.md checkpoint conventions.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

import click

from .config import (
    CONTAINER_TIMEOUT_SECONDS,
    DEFAULT_MAX_COST_USD,
    DEFAULT_MODE,
    DEFAULT_MODELS,
    PLATFORMS,
    RELEASE_PROFILE_PLATFORMS,
    VALID_PLATFORM_NAMES,
)
from .platforms import AdapterError, PlatformAdapter, PullRequest, RunCommit, build_adapter, mask
from .verify import (
    HarnessError,
    InfraFailure,
    RunResult,
    aggregate,
    load_telemetry,
    parse_review_log_line,
    verify_analyzer_findings,
    verify_event_not_degraded,
    verify_model,
    verify_no_failed_agents,
    verify_posting_surfaces,
    verify_summary_marker,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Exit codes
EXIT_PASS = 0
EXIT_PRODUCT_FAILURE = 1
EXIT_INFRA_FAILURE = 2


def validate_platforms(raw: tuple[str, ...] | list[str], *, profile: str | None = None) -> list[str]:
    """Validate a requested platform list. No network I/O.

    - Rejects empty, unknown, and duplicate entries.
    - profile="release" additionally requires the set to be exactly
      {github, gitlab, bitbucket}, no more, no fewer.
    Raises ValueError with a clear message on any violation.
    """
    if not raw:
        raise ValueError("no platforms specified")
    seen: set[str] = set()
    dupes: list[str] = []
    unknown: list[str] = []
    for p in raw:
        if p not in VALID_PLATFORM_NAMES:
            unknown.append(p)
            continue
        if p in seen:
            dupes.append(p)
        seen.add(p)
    if unknown:
        raise ValueError(f"unknown platform(s): {unknown}; valid: {sorted(VALID_PLATFORM_NAMES)}")
    if dupes:
        raise ValueError(f"duplicate platform(s): {dupes}")
    if profile == "release" and seen != RELEASE_PROFILE_PLATFORMS:
        raise ValueError(
            f"profile 'release' requires exactly {sorted(RELEASE_PROFILE_PLATFORMS)}, got {sorted(seen)}"
        )
    return sorted(seen)


@click.group()
def cli() -> None:
    """Deterministic ai-pr-review e2e harness."""


@cli.command()
@click.option("--platforms", "platforms_raw", multiple=True, default=("github", "gitlab", "bitbucket"))
@click.option("--profile", default=None, type=click.Choice(["release"]))
def plan(platforms_raw: tuple[str, ...], profile: str | None) -> None:
    """Offline validation only. No network calls."""
    try:
        resolved = validate_platforms(platforms_raw, profile=profile)
    except ValueError as exc:
        click.echo(f"plan: invalid platform selection: {exc}", err=True)
        sys.exit(EXIT_INFRA_FAILURE)
    click.echo(f"plan: platforms={resolved} profile={profile or '(none)'} -- OK")
    sys.exit(EXIT_PASS)


@cli.command()
@click.option("--platforms", "platforms_raw", multiple=True, default=("github", "gitlab", "bitbucket"))
def preflight(platforms_raw: tuple[str, ...]) -> None:
    """Verify token + repo access for each platform. No LLM calls, no writes."""
    try:
        resolved = validate_platforms(platforms_raw)
    except ValueError as exc:
        click.echo(f"preflight: invalid platform selection: {exc}", err=True)
        sys.exit(EXIT_INFRA_FAILURE)

    failures = []
    for name in resolved:
        adapter = build_adapter(name, dict(os.environ))
        try:
            adapter.preflight()
            click.echo(f"preflight: {name}: OK")
        except AdapterError as exc:
            click.echo(f"preflight: {name}: FAILED: {exc}", err=True)
            failures.append(name)
    sys.exit(EXIT_INFRA_FAILURE if failures else EXIT_PASS)


@cli.command()
@click.option("--from-file", "from_file", type=click.Path(exists=True, path_type=Path), required=True)
def cleanup(from_file: Path) -> None:
    """Idempotent out-of-process cleanup: close + delete-branch every PR/MR
    recorded in an opened.json file (see _record_opened's docstring for why
    this exists -- the in-process signal handler alone isn't a reliable
    enough safety net). Safe to re-run: close()/delete_branch() already
    tolerate an already-gone branch (404/422), and closing an already-closed
    PR/MR is a harmless no-op on all three providers."""
    entries = json.loads(from_file.read_text(encoding="utf-8"))
    failures = []
    for entry in entries:
        # An entry already carries a resolution once `run`'s own pass/fail
        # cleanup logic has decided its fate -- "closed" (already closed on
        # the pass path) or "left_open" (deliberately kept open on a
        # product/infra failure, for debugging, per the leave-open-on-
        # failure policy). Without this check, a run cancelled AFTER that
        # decision but before the process exits (e.g. by a concurrency-group
        # cancel-in-progress triggered by a later push) would have this same
        # `cleanup` invoked against opened.json by e2e.yml's cancel fallback
        # step, closing a PR that was correctly left open moments earlier.
        if entry.get("resolution"):
            continue
        name = str(entry["platform"])
        adapter = build_adapter(name, dict(os.environ))
        pr = PullRequest(
            platform=name, number=int(entry["number"]), branch=str(entry["branch"]), url=str(entry["url"]),
            run_commit=RunCommit(platform=name, run_id="", branch=str(entry["branch"]), commit_sha=""),
        )
        try:
            adapter.close(pr)
        except AdapterError as exc:
            click.echo(f"cleanup: {name} #{pr.number}: FAILED to close: {exc}", err=True)
            failures.append(name)
            continue
        try:
            adapter.delete_branch(pr.branch)
        except AdapterError as exc:
            click.echo(
                f"cleanup: {name} #{pr.number}: closed, but FAILED to delete branch "
                f"{pr.branch!r}: {exc}", err=True,
            )
            failures.append(name)
            continue
        click.echo(f"cleanup: {name} #{pr.number}: closed and branch deleted")
    sys.exit(EXIT_INFRA_FAILURE if failures else EXIT_PASS)


# --- run --------------------------------------------------------------

# Only platforms currently in flight (no result decided yet for this run()
# invocation) -- see `run`'s loop, which prunes an entry the moment
# _run_one_platform returns, whatever the outcome. Without that pruning, a
# signal during platform 2 of a local multi-platform run would also close
# platform 1's PR/MR even when platform 1 had already failed and the
# leave-open-on-failure policy said to keep it for debugging.
_opened_prs: list[tuple[PlatformAdapter, PullRequest]] = []  # for signal-handler cleanup


def _install_signal_handlers() -> None:
    def _handler(signum: int, _frame: object) -> None:
        click.echo(f"\nrun: received signal {signum}; closing any PR/MR opened by this run "
                   "before exiting (a cancelled run has nothing worth debugging)", err=True)
        for adapter, pr in _opened_prs:
            try:
                adapter.close(pr)
                adapter.delete_branch(pr.branch)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup on the way out
                click.echo(f"run: cleanup-on-signal failed for {pr.platform}: {mask(str(exc))}", err=True)
        sys.exit(EXIT_INFRA_FAILURE)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _record_opened(out_dir: Path, platform: str, pr: PullRequest) -> None:
    """Append this run's opened PR/MR to out_dir/opened.json.

    This is the durable, out-of-process record the `cleanup` subcommand
    reads. The in-process signal handler above is the only cleanup path
    today, and it has a real gap: its network calls go through
    _request_with_retry (multiple attempts, tens of seconds of backoff), and
    a CI runner's cancel grace period before SIGKILL can be shorter than
    that, especially if a SIGINT is immediately followed by a SIGTERM
    (re-entering the handler mid-cleanup). Writing this file the moment a
    PR/MR is created means a stuck cleanup can be finished later by anyone
    running `run_e2e cleanup --from-file opened.json`, instead of the PR/MR
    leaking forever with no record of it ever having existed. Also merged
    into result.json (see `run`) so a failed run's CI artifacts name the
    PR/MR to inspect without grepping stderr.
    """
    path = out_dir / "opened.json"
    entries: list[dict[str, str | int]] = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "_record_opened: %s is unreadable/corrupt (%s) -- discarding its prior "
                "entries rather than losing this run's own record of them too. Any "
                "already-opened PR/MR named in the lost entries no longer has a durable "
                "cleanup record.", path, exc,
            )
            entries = []
    entries.append({"platform": platform, "number": pr.number, "branch": pr.branch, "url": pr.url})
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _resolve_opened(out_dir: Path, platform: str, resolution: str) -> None:
    """Mark this run's opened.json entry for *platform* with its final
    resolution ("closed" or "left_open"), so the `cleanup` subcommand (and
    e2e.yml's cancel-on-signal fallback step, which invokes it against this
    same file) knows this entry's fate is already decided and must not be
    touched -- see `cleanup`'s own comment on why that matters for a
    deliberately-left-open failure."""
    path = out_dir / "opened.json"
    if not path.exists():
        return
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("_resolve_opened: %s is unreadable/corrupt (%s) -- cannot record "
                        "%s's resolution", path, exc, platform)
        return
    for entry in entries:
        if entry.get("platform") == platform and "resolution" not in entry:
            entry["resolution"] = resolution
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _write_env_file(path: Path, env_vars: dict[str, str]) -> None:
    """Write a KEY=VALUE env file with 0600 permissions (never world/group
    readable -- this file carries provider API keys and tokens)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for key, value in env_vars.items():
            fh.write(f"{key}={value}\n")


def _run_git(argv: list[str], *, timeout: int = 120, extra_env: dict[str, str] | None = None) -> str:
    """Run a git command via subprocess (argv list, never shell=True).
    Raises InfraFailure (with any embedded credential URL masked) on
    failure or timeout. `extra_env` (the GIT_CONFIG_* auth vars from
    _clone_auth_env, when this call needs them) is merged over the current
    environment rather than replacing it, so PATH/HOME/etc. still resolve."""
    try:
        proc = subprocess.run(
            ["git", *argv], capture_output=True, text=True, timeout=timeout, check=True,
            env={**os.environ, **extra_env} if extra_env else None,
        )
    except subprocess.CalledProcessError as exc:
        raise InfraFailure(mask(f"git {' '.join(argv[:2])} failed: {exc.stderr[:300]}")) from exc
    except subprocess.TimeoutExpired as exc:
        raise InfraFailure(mask(f"git {' '.join(argv[:2])} timed out after {timeout}s: {exc}")) from exc
    except OSError as exc:
        raise InfraFailure(f"git not available on this runner: {exc}") from exc
    return proc.stdout


def _github_reviewer_token() -> str:
    """E2E_GITHUB_REVIEWER_TOKEN falls back to E2E_GITHUB_TOKEN when unset
    OR empty. `os.environ.get(A, os.environ.get(B, ""))` only falls back
    when A is absent -- e2e.yml's `secrets.E2E_GITHUB_REVIEWER_TOKEN` always
    sets the env var, as an empty string when that (documented-optional)
    secret isn't configured, so the old `.get(A, .get(B))` pattern silently
    passed an empty token to git clone / GH_TOKEN in that case instead of
    falling back. `build_adapter()` already used `reviewer_token or
    seeder_token`, matching this; this helper is the single place both the
    clone auth and the container's GH_TOKEN now read from, so they can't
    diverge from that adapter behavior again."""
    return os.environ.get("E2E_GITHUB_REVIEWER_TOKEN") or os.environ.get("E2E_GITHUB_TOKEN", "")


def _clone_auth_env(platform: str) -> tuple[str, dict[str, str]]:
    """Return (credential-free clone URL, extra subprocess env) for the
    given platform.

    Auth is injected via `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_N`/
    `GIT_CONFIG_VALUE_N` (an `http.extraHeader` Basic-auth header), NOT a
    token embedded in the URL. Two reasons, found in the same live-debugging
    session: (1) a URL passed as a subprocess argv element is visible to any
    local user via `ps`/`/proc/<pid>/cmdline` for the life of the clone,
    unlike a value passed via `env=` (still readable via `/proc/<pid>/
    environ`, but that already requires the same privilege as reading the
    process's own memory, a materially narrower default exposure than argv,
    which is often world-readable); (2) it also means `.git/config`'s
    `remote.origin.url` in the cloned workspace never contains the token,
    closing that persistence path too instead of just moving it.
    """
    repo_slug = PLATFORMS[platform].repo_slug
    if platform == "github":
        token = _github_reviewer_token()
        url = f"https://github.com/{repo_slug}.git"
        userpass = f"x-access-token:{token}"
    elif platform == "gitlab":
        token = os.environ.get("E2E_GITLAB_TOKEN", "")
        url = f"https://gitlab.com/{repo_slug}.git"
        userpass = f"oauth2:{token}"
    elif platform == "bitbucket":
        # Git-over-HTTPS with a Bitbucket API token uses the fixed username
        # "x-bitbucket-api-token-auth", NOT the account email -- that's a
        # different scheme from the REST API's Basic auth (email:token),
        # which is what preflight's API calls use. Confirmed live via
        # `git ls-remote` after the email:token form failed with "You may
        # not have access to this repository" despite preflight passing.
        token = os.environ.get("E2E_BITBUCKET_TOKEN", "")
        url = f"https://bitbucket.org/{repo_slug}.git"
        userpass = f"x-bitbucket-api-token-auth:{token}"
    else:
        raise InfraFailure(f"unknown platform {platform!r} for workspace clone")

    auth_header = "Authorization: Basic " + base64.b64encode(userpass.encode()).decode()
    extra_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": auth_header,
    }
    return url, extra_env


def _clone_workspace(platform: str, run_commit: RunCommit, base_ref: str) -> tuple[Path, str]:
    """Shallow-clone the run branch (plus the base ref) into a temp dir for
    mounting at /workspace, and return (workspace_dir, diff_base_sha).

    This is a deliberate, narrow exception to the "no local git clone/push"
    design goal stated elsewhere in this harness: that goal is about the
    WRITE path (creating the run commit), which stays on each provider's
    content API with no local git credentials needed for authoring. A
    read-only clone here is a separate, necessary concern: the product's
    own diff computation (ai_pr_review/diff/compute.py) runs `git -C
    /workspace` *inside the container*, and the container has no way to
    materialize a checkout on its own -- the caller must mount one.

    Auth is passed via env, not a token-embedded URL -- see
    _clone_auth_env's docstring. Any URL/header that still leaks into a git
    error message is masked via mask()'s credential-URL pattern before it's
    ever raised or logged, as defense in depth.

    Verified live against all three platforms as of 2026-09-25, including a
    full 3-platform `workflow_dispatch` run. The Bitbucket leg is what
    originally surfaced the clone-auth-scheme and annotation-pagination
    bugs this module's git history fixes.
    """
    url, clone_env = _clone_auth_env(platform)

    workspace = Path(tempfile.mkdtemp(prefix=f"e2e-ws-{platform}-"))
    try:
        _run_git(["clone", "--quiet", "--depth", "50", "--branch", run_commit.branch,
                  "--single-branch", url, str(workspace)], extra_env=clone_env)
        _run_git(["-C", str(workspace), "fetch", "--quiet", "--depth", "50",
                  "origin", f"{base_ref}:refs/remotes/origin/{base_ref}"], extra_env=clone_env)
        diff_base_sha = _run_git(["-C", str(workspace), "merge-base", "HEAD",
                                   f"origin/{base_ref}"]).strip()
        # The container runs as a fixed non-root uid (1001, per Dockerfile);
        # this host-created checkout must be readable regardless of which
        # uid actually cloned it (a GH-hosted runner and a developer's own
        # machine will differ). No credentials live in .git/config now (see
        # _clone_auth_env), so this world-readable chmod no longer exposes
        # any secret -- only the fixture content itself.
        _run_git(["-C", str(workspace), "config", "--local", "--add", "safe.directory", str(workspace)])
        for root, _dirs, files in os.walk(workspace):
            os.chmod(root, 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return workspace, diff_base_sha


def _provider_env_vars(platform: str, pr: PullRequest) -> dict[str, str]:
    """Build the per-platform env vars ai_pr_review's own env-driven Config
    needs to target this run's PR/MR, per ai_pr_review/vcs/__init__.py's
    documented per-provider requirements (verified against source, not
    guessed): GitHub needs GITHUB_REPOSITORY + PR_NUMBER; GitLab needs a
    project identifier (CI_PROJECT_PATH here), MR_IID, and
    GITLAB_DIFF_BASE_SHA (all three are hard-required -- a run without them
    fails at provider construction, before any review logic runs);
    Bitbucket needs BITBUCKET_WORKSPACE + BITBUCKET_REPO_SLUG + PR_NUMBER.
    """
    repo_slug = PLATFORMS[platform].repo_slug
    if platform == "github":
        return {"VCS_PROVIDER": "github", "GITHUB_REPOSITORY": repo_slug, "PR_NUMBER": str(pr.number)}
    if platform == "gitlab":
        return {"VCS_PROVIDER": "gitlab", "CI_PROJECT_PATH": repo_slug, "MR_IID": str(pr.number)}
    if platform == "bitbucket":
        workspace, _, repo = repo_slug.partition("/")
        return {
            "VCS_PROVIDER": "bitbucket",
            "BITBUCKET_WORKSPACE": workspace,
            "BITBUCKET_REPO_SLUG": repo,
            "PR_NUMBER": str(pr.number),
        }
    raise InfraFailure(f"unknown platform {platform!r} for provider env vars")


_SECRET_ENV_KEYS = {"ANTHROPIC_API_KEY", "GH_TOKEN", "GITLAB_TOKEN", "BITBUCKET_API_TOKEN"}


def _known_secret_values(env_vars: dict[str, str]) -> list[str]:
    """The actual secret values used for this run's container, for a
    literal-value redaction pass in addition to mask()'s pattern matching.
    mask() only catches values that appear in a recognizable shape (after
    "Bearer"/"Basic"/"token"/"PRIVATE-TOKEN", or embedded as URL userinfo);
    a real secret echoed bare (e.g. by an errant `env`/`printenv` in the
    reviewed diff, or a library that logs its own config) would sail
    through untouched. Redacting by literal value is a second, independent
    net under the same artifacts (container.log, telemetry.json) that get
    uploaded from a public repo. Short values are excluded (len < 8) to
    avoid mangling unrelated short text that happens to match an
    unset/placeholder value.
    """
    return [v for k, v in env_vars.items() if k in _SECRET_ENV_KEYS and len(v) >= 8]


def _redact_known_values(text: str, secret_values: list[str]) -> str:
    for value in secret_values:
        text = text.replace(value, "<secret-redacted>")
    return text


def _redact_known_values_in_file(path: Path, secret_values: list[str]) -> None:
    """Best-effort in-place literal-value redaction of a file that will be
    uploaded as a CI artifact. Silently no-ops if the file doesn't exist
    (e.g. the container crashed before writing telemetry.json) -- that
    absence is InfraFailure territory handled elsewhere, not this
    function's concern."""
    if not path.exists() or not secret_values:
        return
    text = path.read_text(encoding="utf-8")
    redacted = _redact_known_values(text, secret_values)
    if redacted != text:
        path.write_text(redacted, encoding="utf-8")


def _run_container(platform: str, pr: PullRequest, workspace: Path, diff_base_sha: str,
                    out_dir: Path, *, mode: str, max_cost_usd: float) -> tuple[int, str]:
    """Run the review container via subprocess (argv list, never shell=True).
    Returns (returncode, redacted_stderr_text). Always docker-rm's the
    container in a finally, regardless of outcome.
    """
    run_id = pr.run_commit.run_id
    container_name = f"e2e-{platform}-{run_id}"
    # Written to a temp file OUTSIDE out_dir, not under it: out_dir is what
    # e2e.yml uploads as a CI artifact (14-day retention, and this repo is
    # public), so a secret-bearing file must never live inside it even
    # transiently. Deleted in `finally` regardless of outcome.
    env_fd, env_file_str = tempfile.mkstemp(prefix=f"e2e-{platform}-", suffix=".env")
    os.close(env_fd)
    env_file = Path(env_file_str)

    env_vars: dict[str, str] = {
        "AI_TELEMETRY_ENABLED": "true",
        "AI_TELEMETRY_SINK": "file:///output/telemetry.json",
        "AI_MAX_COST_USD": str(max_cost_usd),
        "AI_FAIL_ON_COST_CEILING": "true",
        "AI_REVIEW_MODE": mode,
        "ANTHROPIC_API_KEY": os.environ.get("E2E_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""),
        # Both required by ai_pr_review/diff/compute.py's compute_diff(): it
        # builds range_spec as f"origin/{base_ref}...{head_sha}" with no
        # fallback for either being empty -- confirmed live on this
        # harness's first real run, which crashed with a literal
        # "bad revision 'origin/...'" before this fix. base_ref is the bare
        # branch name (compute_diff prepends "origin/" itself, so passing
        # "origin/main" here would double it).
        "BASE_REF": PLATFORMS[platform].base_ref,
        "HEAD_SHA": pr.run_commit.commit_sha,
        **_provider_env_vars(platform, pr),
    }
    if platform == "github":
        env_vars["GH_TOKEN"] = _github_reviewer_token()
    elif platform == "gitlab":
        env_vars["GITLAB_TOKEN"] = os.environ.get("E2E_GITLAB_TOKEN", "")
        env_vars["GITLAB_DIFF_BASE_SHA"] = diff_base_sha
    elif platform == "bitbucket":
        env_vars["BITBUCKET_EMAIL"] = os.environ.get("E2E_BITBUCKET_EMAIL", "")
        env_vars["BITBUCKET_API_TOKEN"] = os.environ.get("E2E_BITBUCKET_TOKEN", "")

    # CI builds a fresh candidate image from the PR's own code and must test
    # THAT image, not whatever is already published -- that is the entire
    # point of a release gate. AI_PR_REVIEW_E2E_IMAGE lets the caller (the
    # e2e workflow's matrix step, or a developer testing a local build)
    # point at that candidate tag. It only falls back to the published
    # :dev tag for an ad hoc local smoke-check against what's already live.
    image = os.environ.get("AI_PR_REVIEW_E2E_IMAGE", "ghcr.io/tag1consulting/ai-pr-review:dev")
    try:
        _write_env_file(env_file, env_vars)
        argv = [
            "docker", "run", "--rm=false",  # explicit rm in finally, not --rm, so we control timing
            "--name", container_name,
            "--env-file", str(env_file),
            "-v", f"{workspace}:/workspace",
            "-v", f"{out_dir}:/output",
            image,
        ]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=CONTAINER_TIMEOUT_SECONDS,
            )
            returncode = proc.returncode
            stderr_text = proc.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            stderr_text = f"container timed out after {CONTAINER_TIMEOUT_SECONDS}s: {exc}"
        except OSError as exc:
            # e.g. FileNotFoundError if `docker` isn't installed. Previously
            # only TimeoutExpired was caught here, so this would have
            # propagated uncaught out of _run_one_platform's try block
            # (which doesn't wrap this call), crashing the whole run
            # instead of being classified as this platform's infra_failure.
            returncode = -1
            stderr_text = f"failed to invoke docker: {exc}"
        finally:
            rm_proc = subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
            if rm_proc.returncode != 0:
                # Best-effort cleanup; a failure here (e.g. runner resource
                # pressure) shouldn't fail the run, but it should be visible
                # instead of silently leaving a stray container behind.
                click.echo(
                    f"run: {platform}: warning: 'docker rm -f {container_name}' failed "
                    f"(rc={rm_proc.returncode}): {mask(rm_proc.stderr[:300])}", err=True,
                )
    finally:
        env_file.unlink(missing_ok=True)

    secret_values = _known_secret_values(env_vars)
    redacted = _redact_known_values(mask(stderr_text), secret_values)
    (out_dir / f"{platform}.container.log").write_text(redacted, encoding="utf-8")
    _redact_known_values_in_file(out_dir / "telemetry.json", secret_values)
    return returncode, redacted


def _run_one_platform(name: str, run_id: str, out_dir: Path, *, mode: str, max_cost_usd: float) -> RunResult:
    adapter = build_adapter(name, dict(os.environ))
    platform_out_dir = out_dir / name
    platform_out_dir.mkdir(parents=True, exist_ok=True)
    # uid 1001 (the container's fixed non-root user, per Dockerfile) must be
    # able to write telemetry.json here regardless of the host uid that
    # created this directory -- GH-hosted runners happen to also be uid
    # 1001, which masked this on CI, but a local run under a different uid
    # would otherwise fail every time with a misleading InfraFailure. Mode
    # 0o1733 (not 0o777): the sticky bit stops another local user on a
    # shared host from renaming/deleting files they don't own in this
    # world-writable directory (the standard /tmp-style mitigation) --
    # relevant to local multi-user dev, not GH-hosted single-tenant runners.
    os.chmod(platform_out_dir, 0o1733)

    try:
        adapter.preflight()
        run_commit = adapter.create_run_commit(run_id)
        pr = adapter.open_pr(run_commit)
        _opened_prs.append((adapter, pr))
        # Written immediately, independent of _opened_prs (an in-process
        # list a SIGKILL mid-cleanup loses entirely): the sole out-of-process
        # cleanup fallback -- a `cleanup` subcommand -- reads this file to
        # find PRs/branches an in-process signal handler didn't get to
        # close before the runner's cancel grace period expired.
        _record_opened(out_dir, name, pr)
    except AdapterError as exc:
        return aggregate(name, [], infra_error=InfraFailure(str(exc)))

    try:
        workspace, diff_base_sha = _clone_workspace(name, run_commit, PLATFORMS[name].base_ref)
    except HarnessError as exc:
        _resolve_opened(out_dir, name, "left_open")
        return aggregate(name, [], infra_error=exc)

    try:
        returncode, stderr_text = _run_container(
            name, pr, workspace, diff_base_sha, platform_out_dir, mode=mode, max_cost_usd=max_cost_usd,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    if returncode != 0:
        # The only prior signals were a regex match against stderr and
        # telemetry's presence -- neither of which reflects the process's
        # own exit status. A crash after partially flushing telemetry, or
        # after emitting the completion line but failing during cleanup,
        # was previously invisible to the harness and could still score a
        # pass. This must be checked before trusting anything else below.
        _resolve_opened(out_dir, name, "left_open")
        return aggregate(name, [], infra_error=InfraFailure(
            f"review container exited {returncode}: {stderr_text[-500:]}"))

    try:
        log_result = parse_review_log_line(stderr_text)
        if log_result.skipped:
            raise InfraFailure(f"review was skipped: {log_result.skip_reason}")

        telemetry = load_telemetry(platform_out_dir / "telemetry.json")

        evidence = adapter.fetch_summary(pr)
        adapter.fetch_inline(pr, evidence)
        # fetch_annotations is a documented no-op on every adapter except
        # Bitbucket's (_BaseAdapter's default), so calling it unconditionally
        # here is correct for every platform, not just a Bitbucket special
        # case -- no per_finding_surface check needed for this one.
        adapter.fetch_annotations(pr, evidence)

        verdicts = [
            verify_summary_marker(evidence, run_commit.commit_sha),
            verify_no_failed_agents(log_result, telemetry),
            verify_event_not_degraded(
                log_result.event or "", telemetry=telemetry, summary_body=evidence.summary_body,
            ),
            verify_model(telemetry, DEFAULT_MODELS["anthropic"][0]),
            *verify_posting_surfaces(evidence, per_finding_surface=PLATFORMS[name].per_finding_surface),
            verify_analyzer_findings(
                evidence, list(PLATFORMS[name].expected_findings),
                findings_floor=PLATFORMS[name].findings_floor,
                per_finding_surface=PLATFORMS[name].per_finding_surface,
            ),
        ]
    except HarnessError as exc:
        _resolve_opened(out_dir, name, "left_open")
        return aggregate(name, [], infra_error=exc)
    except AdapterError as exc:
        _resolve_opened(out_dir, name, "left_open")
        return aggregate(name, [], infra_error=InfraFailure(str(exc)))

    result = aggregate(name, verdicts)

    # Cleanup policy: on pass, close + delete branch, confirming each step.
    # On fail (product or infra), leave everything open -- no janitor.
    if result.ok:
        try:
            adapter.close(pr)
            adapter.delete_branch(pr.branch)
            click.echo(f"run: {name}: pass -- closed PR/MR {pr.url} and deleted branch {pr.branch}")
            _resolve_opened(out_dir, name, "closed")
        except AdapterError as exc:
            # A cleanup failure on the pass path is itself an infra failure
            # per the exit-code contract. Left unresolved in opened.json
            # (neither "closed" nor "left_open" cleanly describes it): the
            # `cleanup` subcommand should still retry a genuinely unresolved
            # close/delete-branch failure like this one. Logged here (not
            # just embedded in the returned RunResult's reasons tuple),
            # since whether that tuple reaches an operator depends on how
            # aggregate()/reporting formats and truncates it downstream --
            # this warning is visible in the job log regardless.
            logger.warning("run: %s: cleanup failed after pass: %s", name, mask(str(exc)))
            return RunResult(platform=name, category="infra_failure", verdicts=result.verdicts,
                              reasons=(f"cleanup failed after pass: {exc}",))
    else:
        click.echo(f"run: {name}: {result.category} -- leaving PR/MR {pr.url} open for inspection", err=True)
        _resolve_opened(out_dir, name, "left_open")

    return result


@cli.command()
@click.option("--platforms", "platforms_raw", multiple=True, default=("github", "gitlab", "bitbucket"))
@click.option("--mode", type=click.Choice(["full", "quick"]), default=DEFAULT_MODE)
@click.option("--out-dir", type=click.Path(path_type=Path), default=None)
@click.option("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
def run(platforms_raw: tuple[str, ...], mode: str, out_dir: Path | None, max_cost_usd: float) -> None:
    """Run the full e2e harness against real platforms. Costs real API money."""
    try:
        resolved = validate_platforms(platforms_raw)
    except ValueError as exc:
        click.echo(f"run: invalid platform selection: {exc}", err=True)
        sys.exit(EXIT_INFRA_FAILURE)

    _install_signal_handlers()

    run_id = uuid.uuid4().hex[:12]
    out_dir = out_dir or (REPO_ROOT / "tests" / "e2e" / ".runs" / run_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, RunResult] = {}
    for name in resolved:
        results[name] = _run_one_platform(name, run_id, out_dir, mode=mode, max_cost_usd=max_cost_usd)
        # This platform's outcome is now decided -- prune it from the
        # signal-handler's list so a later platform's Ctrl-C/SIGTERM can't
        # also close THIS one's PR/MR, contradicting the leave-open-on-
        # failure policy for a platform that already finished and failed.
        _opened_prs[:] = [(a, p) for a, p in _opened_prs if p.platform != name]

    opened_by_platform: dict[str, dict[str, object]] = {}
    opened_path = out_dir / "opened.json"
    if opened_path.exists():
        try:
            for entry in json.loads(opened_path.read_text(encoding="utf-8")):
                opened_by_platform[str(entry["platform"])] = entry
        except (json.JSONDecodeError, OSError) as exc:
            # result.json still gets written below, just without pr_url/
            # branch for any platform -- logged so that gap is traceable to
            # a corrupt/unreadable opened.json rather than reading as "no
            # PR was ever opened" to whoever inspects result.json later.
            logger.warning("run: %s is unreadable/corrupt (%s) -- result.json will be missing "
                           "pr_url/branch for every platform", opened_path, exc)

    result_payload = {
        name: {
            "category": r.category, "reasons": list(r.reasons),
            "verdicts": [asdict(v) for v in r.verdicts],
            **({"pr_url": opened_by_platform[name]["url"], "branch": opened_by_platform[name]["branch"]}
               if name in opened_by_platform else {}),
        }
        for name, r in results.items()
    }
    (out_dir / "result.json").write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

    summary_lines = [f"# e2e run {run_id}", ""]
    for name, r in results.items():
        line = f"- **{name}**: {r.category}" + (f": {'; '.join(r.reasons)}" if r.reasons else "")
        if not r.ok and name in opened_by_platform:
            line += f" -- {opened_by_platform[name]['url']}"
        summary_lines.append(line)
    summary_text = "\n".join(summary_lines) + "\n"
    (out_dir / "summary.md").write_text(summary_text, encoding="utf-8")

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary_text)

    click.echo(summary_text)

    if any(r.category == "infra_failure" for r in results.values()):
        sys.exit(EXIT_INFRA_FAILURE)
    if any(r.category == "product_failure" for r in results.values()):
        sys.exit(EXIT_PRODUCT_FAILURE)
    sys.exit(EXIT_PASS)


if __name__ == "__main__":
    cli()
