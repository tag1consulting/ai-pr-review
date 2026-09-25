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
import signal
import subprocess
import sys
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
from .platforms import AdapterError, PlatformAdapter, PullRequest, build_adapter, mask
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


def _run_container(platform: str, run_id: str, out_dir: Path, *, mode: str, max_cost_usd: float) -> tuple[int, str]:
    """Run the review container via subprocess (argv list, never shell=True).
    Returns (returncode, redacted_stderr_text). Always docker-rm's the
    container in a finally, regardless of outcome.
    """
    container_name = f"e2e-{platform}-{run_id}"
    env_file = out_dir / f"{platform}.env"

    env_vars = {
        "AI_TELEMETRY_ENABLED": "true",
        "AI_TELEMETRY_SINK": "file:///output/telemetry.json",
        "AI_MAX_COST_USD": str(max_cost_usd),
        "AI_FAIL_ON_COST_CEILING": "true",
        "AI_REVIEW_MODE": mode,
        "ANTHROPIC_API_KEY": os.environ.get("E2E_ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")),
    }
    if platform == "github":
        env_vars["GH_TOKEN"] = os.environ.get("E2E_GITHUB_REVIEWER_TOKEN", os.environ.get("E2E_GITHUB_TOKEN", ""))
    elif platform == "gitlab":
        env_vars["GITLAB_TOKEN"] = os.environ.get("E2E_GITLAB_TOKEN", "")
    elif platform == "bitbucket":
        env_vars["BITBUCKET_EMAIL"] = os.environ.get("E2E_BITBUCKET_EMAIL", "")
        env_vars["BITBUCKET_API_TOKEN"] = os.environ.get("E2E_BITBUCKET_TOKEN", "")

    _write_env_file(env_file, env_vars)

    # CI builds a fresh candidate image from the PR's own code and must test
    # THAT image, not whatever is already published -- that is the entire
    # point of a release gate. AI_PR_REVIEW_E2E_IMAGE lets the caller (the
    # e2e workflow's matrix step, or a developer testing a local build)
    # point at that candidate tag. It only falls back to the published
    # :dev tag for an ad hoc local smoke-check against what's already live.
    image = os.environ.get("AI_PR_REVIEW_E2E_IMAGE", "ghcr.io/tag1consulting/ai-pr-review:dev")
    argv = [
        "docker", "run", "--rm=false",  # explicit rm in finally, not --rm, so we control timing
        "--name", container_name,
        "--env-file", str(env_file),
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
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)

    redacted = mask(stderr_text)
    (out_dir / f"{platform}.container.log").write_text(redacted, encoding="utf-8")
    return returncode, redacted


def _run_one_platform(name: str, run_id: str, out_dir: Path, *, mode: str, max_cost_usd: float) -> RunResult:
    adapter = build_adapter(name, dict(os.environ))
    platform_out_dir = out_dir / name
    platform_out_dir.mkdir(parents=True, exist_ok=True)

    try:
        adapter.preflight()
        run_commit = adapter.create_run_commit(run_id)
        pr = adapter.open_pr(run_commit)
        _opened_prs.append((adapter, pr))
    except AdapterError as exc:
        return aggregate(name, [], infra_error=InfraFailure(str(exc)))

    returncode, stderr_text = _run_container(name, run_id, platform_out_dir, mode=mode, max_cost_usd=max_cost_usd)

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
        summary_lines.append(f"- **{name}**: {r.category}" + (f" — {'; '.join(r.reasons)}" if r.reasons else ""))
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
