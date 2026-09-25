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

import json
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


# --- run --------------------------------------------------------------

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


def _write_env_file(path: Path, env_vars: dict[str, str]) -> None:
    """Write a KEY=VALUE env file with 0600 permissions (never world/group
    readable -- this file carries provider API keys and tokens)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for key, value in env_vars.items():
            fh.write(f"{key}={value}\n")


def _run_git(argv: list[str], *, timeout: int = 120) -> str:
    """Run a git command via subprocess (argv list, never shell=True).
    Raises InfraFailure (with any embedded credential URL masked) on
    failure or timeout."""
    try:
        proc = subprocess.run(
            ["git", *argv], capture_output=True, text=True, timeout=timeout, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise InfraFailure(mask(f"git {' '.join(argv[:2])} failed: {exc.stderr[:300]}")) from exc
    except subprocess.TimeoutExpired as exc:
        raise InfraFailure(mask(f"git {' '.join(argv[:2])} timed out after {timeout}s: {exc}")) from exc
    except OSError as exc:
        raise InfraFailure(f"git not available on this runner: {exc}") from exc
    return proc.stdout


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

    Uses a token-embedded HTTPS URL. The token never reaches disk outside
    this process's argv (subprocess argv, not a shell string), and any URL
    that leaks into a git error message is masked via mask()'s
    credential-URL pattern before it's ever raised or logged.

    Unverified: this whole clone-and-mount path has not been exercised
    against a live run. It exists to satisfy a real product requirement
    (diff/compute.py needs a checkout) discovered by review after the
    initial harness build, not something confirmed working end-to-end.
    """
    repo_slug = PLATFORMS[platform].repo_slug
    if platform == "github":
        token = os.environ.get("E2E_GITHUB_REVIEWER_TOKEN", os.environ.get("E2E_GITHUB_TOKEN", ""))
        url = f"https://x-access-token:{token}@github.com/{repo_slug}.git"
    elif platform == "gitlab":
        token = os.environ.get("E2E_GITLAB_TOKEN", "")
        url = f"https://oauth2:{token}@gitlab.com/{repo_slug}.git"
    elif platform == "bitbucket":
        email = os.environ.get("E2E_BITBUCKET_EMAIL", "")
        token = os.environ.get("E2E_BITBUCKET_TOKEN", "")
        url = f"https://{email}:{token}@bitbucket.org/{repo_slug}.git"
    else:
        raise InfraFailure(f"unknown platform {platform!r} for workspace clone")

    workspace = Path(tempfile.mkdtemp(prefix=f"e2e-ws-{platform}-"))
    try:
        _run_git(["clone", "--quiet", "--depth", "50", "--branch", run_commit.branch,
                  "--single-branch", url, str(workspace)])
        _run_git(["-C", str(workspace), "fetch", "--quiet", "--depth", "50",
                  "origin", f"{base_ref}:refs/remotes/origin/{base_ref}"])
        diff_base_sha = _run_git(["-C", str(workspace), "merge-base", "HEAD",
                                   f"origin/{base_ref}"]).strip()
        # The container runs as a fixed non-root uid (1001, per Dockerfile);
        # this host-created checkout must be readable regardless of which
        # uid actually cloned it (a GH-hosted runner and a developer's own
        # machine will differ).
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
        "ANTHROPIC_API_KEY": os.environ.get("E2E_ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")),
        **_provider_env_vars(platform, pr),
    }
    if platform == "github":
        env_vars["GH_TOKEN"] = os.environ.get("E2E_GITHUB_REVIEWER_TOKEN", os.environ.get("E2E_GITHUB_TOKEN", ""))
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

    redacted = mask(stderr_text)
    (out_dir / f"{platform}.container.log").write_text(redacted, encoding="utf-8")
    return returncode, redacted


def _run_one_platform(name: str, run_id: str, out_dir: Path, *, mode: str, max_cost_usd: float) -> RunResult:
    adapter = build_adapter(name, dict(os.environ))
    platform_out_dir = out_dir / name
    platform_out_dir.mkdir(parents=True, exist_ok=True)
    # uid 1001 (the container's fixed non-root user, per Dockerfile) must be
    # able to write telemetry.json here regardless of the host uid that
    # created this directory -- GH-hosted runners happen to also be uid
    # 1001, which masked this on CI, but a local run under a different uid
    # would otherwise fail every time with a misleading InfraFailure.
    os.chmod(platform_out_dir, 0o777)

    try:
        adapter.preflight()
        run_commit = adapter.create_run_commit(run_id)
        pr = adapter.open_pr(run_commit)
        _opened_prs.append((adapter, pr))
    except AdapterError as exc:
        return aggregate(name, [], infra_error=InfraFailure(str(exc)))

    try:
        workspace, diff_base_sha = _clone_workspace(name, run_commit, PLATFORMS[name].base_ref)
    except HarnessError as exc:
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
        return aggregate(name, [], infra_error=InfraFailure(
            f"review container exited {returncode}: {stderr_text[-500:]}"))

    try:
        log_result = parse_review_log_line(stderr_text)
        if log_result.skipped:
            raise InfraFailure(f"review was skipped: {log_result.skip_reason}")

        telemetry = load_telemetry(platform_out_dir / "telemetry.json")

        evidence = adapter.fetch_summary(pr)
        adapter.fetch_inline(pr, evidence)
        if name == "bitbucket":
            adapter.fetch_annotations(pr, evidence)

        verdicts = [
            verify_summary_marker(evidence, run_commit.commit_sha),
            verify_no_failed_agents(log_result, telemetry),
            verify_event_not_degraded(
                # expected_event is the harness's own request, not read back
                # from telemetry -- this run relies on the default
                # AI_APPROVAL_CEILING ("approve"), so a clean run should post
                # APPROVE or REQUEST_CHANGES, never a degraded plain COMMENT.
                log_result.event or "", "APPROVE",
                telemetry=telemetry, summary_body=evidence.summary_body,
            ),
            verify_model(telemetry, DEFAULT_MODELS["anthropic"][0]),
            *verify_posting_surfaces(evidence, bitbucket=(name == "bitbucket")),
            verify_analyzer_findings(
                evidence, list(PLATFORMS[name].expected_findings),
                findings_floor=PLATFORMS[name].findings_floor,
            ),
        ]
    except HarnessError as exc:
        return aggregate(name, [], infra_error=exc)
    except AdapterError as exc:
        return aggregate(name, [], infra_error=InfraFailure(str(exc)))

    result = aggregate(name, verdicts)

    # Cleanup policy: on pass, close + delete branch, confirming each step.
    # On fail (product or infra), leave everything open -- no janitor.
    if result.ok:
        try:
            adapter.close(pr)
            adapter.delete_branch(pr.branch)
            click.echo(f"run: {name}: pass -- closed PR/MR {pr.url} and deleted branch {pr.branch}")
        except AdapterError as exc:
            # A cleanup failure on the pass path is itself an infra failure
            # per the exit-code contract.
            return RunResult(platform=name, category="infra_failure", verdicts=result.verdicts,
                              reasons=(f"cleanup failed after pass: {exc}",))
    else:
        click.echo(f"run: {name}: {result.category} -- leaving PR/MR {pr.url} open for inspection", err=True)

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

    result_payload = {name: {"category": r.category, "reasons": list(r.reasons),
                              "verdicts": [asdict(v) for v in r.verdicts]}
                       for name, r in results.items()}
    (out_dir / "result.json").write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

    summary_lines = [f"# e2e run {run_id}", ""]
    for name, r in results.items():
        summary_lines.append(f"- **{name}**: {r.category}" + (f": {'; '.join(r.reasons)}" if r.reasons else ""))
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
