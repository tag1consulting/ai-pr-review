"""Story 17-9: fork-safe analyzers, base-ref suppressions, the persisted-credential check, and
the passthrough and tool-loop rules for a fork run."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from ai_pr_review.analyzers import _env
from ai_pr_review.analyzers.bridge import (
    _ANALYZERS,
    ANALYZER_NAMES,
    FORK_RUN_ANALYZERS,
    FORK_SKIPPED_ANALYZERS,
    AnalyzerSpec,
    fork_skipped_eligible,
    run_analyzers,
)
from ai_pr_review.analyzers.native import ruff as ruff_mod
from ai_pr_review.analyzers.native import trufflehog as trufflehog_mod
from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.suppress import apply_suppressions, load_rules
from ai_pr_review.manifest import ChangedFiles
from ai_pr_review.review import fork_gate
from ai_pr_review.review.fork_gate import ForkGateFacts, persisted_credential_reason
from ai_pr_review.review.not_reviewed import build_not_reviewed_notice
from ai_pr_review.review.runtime import (
    ReviewRuntime,
    SkipPlan,
    _apply_fork_gate,
    build_review_runtime,
)

from .test_runtime import _FakeProvider, _make_config, _make_diff_result

SHA = "a" * 40
_REAL_PERSISTED = persisted_credential_reason  # before the fixture patches it
_REAL_WORKSPACE_HEAD = fork_gate.workspace_head


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Any:
    for var in fork_gate.LLM_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(fork_gate, "workspace_head", lambda workspace=".": SHA)
    monkeypatch.setattr(fork_gate, "persisted_credential_reason", lambda workspace=".": "")
    _env.configure_passthrough((), allow_passthrough=True)
    _env.configure_fork_isolation(False)
    yield
    _env.configure_passthrough((), allow_passthrough=True)
    _env.configure_fork_isolation(False)


def _git(cwd: Path, *args: str) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


# --- the skip list and the run list ---------------------------------------------------------------


def test_the_fork_lists_match_decisions_md_and_cover_every_analyzer() -> None:
    assert {"eslint", "golangci-lint", "semgrep", "shellcheck", "hadolint", "kube-linter"} == FORK_SKIPPED_ANALYZERS
    assert {
        "phpcs", "phpstan", "checkov", "tflint", "ruff", "trufflehog", "docs-api-check",
        "docs-ref-check", "docs-drift-check", "dep-exists", "cve-check", "suppression-check"} == FORK_RUN_ANALYZERS
    assert FORK_SKIPPED_ANALYZERS | FORK_RUN_ANALYZERS == ANALYZER_NAMES  # a new analyzer must be classified
    assert not FORK_SKIPPED_ANALYZERS & FORK_RUN_ANALYZERS


def _fake_specs(calls: list[str]) -> list[AnalyzerSpec]:
    def make(name: str) -> Any:
        def fn(cf: ChangedFiles, diff: Path) -> list[Finding]:
            calls.append(name)
            return []
        return fn
    return [AnalyzerSpec(n, [], make(n)) for n in ("eslint", "shellcheck", "semgrep", "ruff", "trufflehog")]


@pytest.mark.parametrize(("fork_run", "expected"), [
    (True, ["ruff", "trufflehog"]),
    (False, ["eslint", "shellcheck", "semgrep", "ruff", "trufflehog"]),
])
def test_a_fork_run_launches_only_the_fork_safe_analyzers(
    monkeypatch: pytest.MonkeyPatch, fork_run: bool, expected: list[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("ai_pr_review.analyzers.bridge._ANALYZERS", _fake_specs(calls))
    anyio.run(lambda: run_analyzers(ChangedFiles(), "d.txt", fork_run=fork_run))  # type: ignore[arg-type]
    assert sorted(calls) == sorted(expected)


def test_the_skipped_names_list_only_analyzers_that_had_files() -> None:
    cf = ChangedFiles(all_files=["a.js", "b.sh", "c.py"], js_ts=["a.js"], shell=["b.sh"], python=["c.py"])
    assert fork_skipped_eligible(cf) == ["eslint", "semgrep", "shellcheck"]  # semgrep has no file gate
    assert fork_skipped_eligible(cf, frozenset({"eslint", "semgrep"})) == ["shellcheck"]  # already off by config
    assert fork_skipped_eligible(ChangedFiles(python=["c.py"])) == ["semgrep"]
    assert "semgrep" in {s.name for s in _ANALYZERS}


def test_the_notice_names_the_skipped_analyzers_and_stays_silent_without_any() -> None:
    notice = build_not_reviewed_notice(
        excluded_patterns=[], skipped_agents=[], is_incremental=False, skipped_analyzers=["shellcheck", "eslint"])
    assert "`eslint`, `shellcheck`" in notice and "fork run" in notice
    assert build_not_reviewed_notice(excluded_patterns=[], skipped_agents=[], is_incremental=False) == ""


# --- ruff and trufflehog take no config from the tree ---------------------------------------------


def _ruff_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ruff_mod.shutil, "which", lambda name: "/usr/bin/ruff")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(ruff_mod, "run_cli_json_analyzer", lambda **kw: seen.update(kw) or [])
    ruff_mod._run_ruff(ChangedFiles(python=["a.py"]), tmp_path / "diff.txt")
    return list(seen["command"])


def test_ruff_runs_isolated_on_a_fork_run_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert "--isolated" not in _ruff_command(monkeypatch, tmp_path)
    _env.configure_fork_isolation(True)
    assert "--isolated" in _ruff_command(monkeypatch, tmp_path)


def _trufflehog_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[list[str], list[Finding]]:
    (tmp_path / "a.txt").write_text("x\n")
    (tmp_path / ".trufflehog.yml").write_text("exclude_paths: [a.txt]\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trufflehog_mod.shutil, "which", lambda name: "/usr/bin/trufflehog")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(trufflehog_mod.subprocess, "run", run)
    findings = trufflehog_mod._run_trufflehog(ChangedFiles(all_files=["a.txt"]), tmp_path / "diff.txt")
    return list(run.call_args.args[0]), findings


def test_trufflehog_ignores_the_repo_config_and_allowlist_on_a_fork_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cmd, _ = _trufflehog_command(monkeypatch, tmp_path)
    assert "--config" in cmd
    _env.configure_fork_isolation(True)
    cmd, _ = _trufflehog_command(monkeypatch, tmp_path)
    assert "--config" not in cmd


def test_the_trufflehog_allowlist_is_not_loaded_on_a_fork_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loaded: list[Path] = []
    monkeypatch.setattr(trufflehog_mod, "_load_allowlist", lambda ws: loaded.append(ws) or set())
    _env.configure_fork_isolation(True)
    _trufflehog_command(monkeypatch, tmp_path)
    assert loaded == []


# --- suppressions ---------------------------------------------------------------------------------


def _finding(**kw: Any) -> Finding:
    return Finding.model_validate({"severity": "Low", "confidence": 60, "finding": "needle", "source": "t", **kw})


def _rules_file(directory: Path, rules: list[Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "suppressions.json").write_text(json.dumps(rules))


def _rule(rule_id: str, pattern: str) -> dict[str, Any]:
    return {"id": rule_id, "reason": "r", "match": {"pattern": pattern}}


def test_a_bad_regex_drops_only_that_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "script"
    _rules_file(script / "config", [_rule("global-ok", "needle")])
    ws = tmp_path / "ws"
    _rules_file(ws / ".ai-pr-review", [_rule("bad", "("), _rule("local-ok", "other")])
    rules = load_rules(str(script), workspace=str(ws))
    assert [r.id for r in rules] == ["global-ok", "local-ok"]
    err = capsys.readouterr().err
    assert "'bad'" in err and "The other rules stay" in err
    kept, _ = apply_suppressions([_finding()], rules)
    assert kept == []  # the global rule still works


@pytest.mark.parametrize("bad", [{"id": "x", "match": "not-a-dict"}, {"id": "y", "match": {"pattern": 5}}])
def test_a_rule_of_the_wrong_shape_is_dropped_alone(tmp_path: Path, bad: dict[str, Any]) -> None:
    script = tmp_path / "script"
    _rules_file(script / "config", [bad, _rule("ok", "needle")])
    assert [r.id for r in load_rules(str(script))] == ["ok"]


def _base_ref_repo(tmp_path: Path) -> Path:
    ws = _repo(tmp_path / "ws")
    _rules_file(ws / ".ai-pr-review", [_rule("base-rule", "needle")])
    _git(ws, "add", ".")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    _git(ws, "update-ref", "refs/remotes/origin/main", "HEAD")
    # The fork tree rewrites the file and adds a rule that would silence everything.
    _rules_file(ws / ".ai-pr-review", [_rule("fork-rule", ".*")])
    return ws


def test_a_fork_run_reads_local_suppressions_from_the_base_ref_not_the_tree(tmp_path: Path) -> None:
    ws = _base_ref_repo(tmp_path)
    script = tmp_path / "script"
    _rules_file(script / "config", [])
    from_tree = load_rules(str(script), workspace=str(ws))
    assert [r.id for r in from_tree] == ["fork-rule"]  # a normal run keeps reading the workspace
    from_base = load_rules(str(script), workspace=str(ws), base_ref="main", local_source="base-ref")
    assert [r.id for r in from_base] == ["base-rule"]


@pytest.mark.parametrize("base_ref", ["", "no-such-branch"])
def test_a_fork_run_with_no_readable_base_file_gets_the_global_rules_only(tmp_path: Path, base_ref: str) -> None:
    ws = _base_ref_repo(tmp_path)
    script = tmp_path / "script"
    _rules_file(script / "config", [_rule("global", "x")])
    rules = load_rules(str(script), workspace=str(ws), base_ref=base_ref, local_source="base-ref")
    assert [r.id for r in rules] == ["global"]


# --- the persisted-credential check ---------------------------------------------------------------


def _clean_repo(tmp_path: Path) -> Path:
    ws = _repo(tmp_path / "w")
    _git(ws, "remote", "add", "origin", "https://github.com/o/r")
    return ws


def test_a_clean_workspace_has_no_credential_reason(tmp_path: Path) -> None:
    assert _REAL_PERSISTED(str(_clean_repo(tmp_path))) == ""
    _git(tmp_path / "w", "remote", "set-url", "origin", "git@github.com:o/r.git")
    assert _REAL_PERSISTED(str(tmp_path / "w")) == ""


@pytest.mark.parametrize(("key", "value"), [
    ("http.https://github.com/.extraheader", "AUTHORIZATION: basic ZHVtbXk="),
    ("credential.helper", "store"),
    ("includeIf.gitdir:/home/runner/work/.path", "/tmp/creds"),
])
def test_a_persisted_credential_setting_gives_a_reason_that_names_the_key_only(
    tmp_path: Path, key: str, value: str,
) -> None:
    ws = _clean_repo(tmp_path)
    _git(ws, "config", "--local", key, value)
    reason = _REAL_PERSISTED(str(ws))
    assert "persist-credentials: false" in reason and key.split(":")[0].lower()[:10] in reason.lower()
    assert value not in reason and "ZHVtbXk" not in reason


@pytest.mark.parametrize("url", [
    "https://x-access-token:dummy-value@github.com/o/r", "https://dummytoken@github.com/o/r"])
def test_a_token_in_a_remote_url_gives_a_reason_without_the_url(tmp_path: Path, url: str) -> None:
    ws = _clean_repo(tmp_path)
    _git(ws, "remote", "set-url", "origin", url)
    reason = _REAL_PERSISTED(str(ws))
    assert "remote.origin.url" in reason and "dummy" not in reason


def test_a_token_in_a_push_url_is_found_too(tmp_path: Path) -> None:
    ws = _clean_repo(tmp_path)
    _git(ws, "remote", "set-url", "--push", "origin", "https://u:dummy@github.com/o/r")
    assert "pushurl" in _REAL_PERSISTED(str(ws))


def test_a_credential_in_an_included_file_is_found(tmp_path: Path) -> None:
    ws = _clean_repo(tmp_path)
    extra = tmp_path / "extra.cfg"
    extra.write_text('[http "https://github.com/"]\n\textraheader = AUTHORIZATION: basic ZHVtbXk=\n')
    _git(ws, "config", "--local", "include.path", str(extra))
    assert "extraheader" in _REAL_PERSISTED(str(ws))


def test_a_workspace_git_cannot_read_pauses_the_run(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert "could not be checked" in _REAL_PERSISTED(str(plain))


def test_the_check_fails_closed_when_git_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = _clean_repo(tmp_path)

    def boom(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("git")
    monkeypatch.setattr(fork_gate.subprocess, "run", boom)
    assert "could not be checked" in _REAL_PERSISTED(str(ws))


def test_the_check_runs_with_a_minimal_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = _clean_repo(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "canary-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "canary-key")
    seen: list[dict[str, str]] = []
    real = fork_gate.subprocess.run

    def spy(cmd: Any, **kw: Any) -> Any:
        seen.append(kw["env"])
        return real(cmd, **kw)

    monkeypatch.setattr(fork_gate.subprocess, "run", spy)
    _REAL_PERSISTED(str(ws))
    assert seen and all("GH_TOKEN" not in e and "ANTHROPIC_API_KEY" not in e for e in seen)


class _P(_FakeProvider):
    """A provider fake with fork facts."""

    facts: ForkGateFacts | None = None

    def get_fork_gate_facts(self, head_sha: str, context: str, *, read_approval: bool = True) -> ForkGateFacts | None:
        return self.facts


def _provider(association: str, head_repo: str = "mallory/r") -> _P:
    p = _P()
    p.facts = ForkGateFacts(head_repo, "o/r", "", "mallory", association)
    return p


def _gate_cfg(**kw: Any) -> Any:
    return _make_config(event_name="pull_request_target", head_sha=SHA, fork_policy="auto", **kw)


def test_a_planted_credential_pauses_a_fork_run_and_a_clean_one_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ws = _clean_repo(tmp_path)
    monkeypatch.chdir(ws)
    monkeypatch.setattr(fork_gate, "persisted_credential_reason", _REAL_PERSISTED)
    _, skip, profile = _apply_fork_gate(_gate_cfg(), _provider("CONTRIBUTOR"))  # type: ignore[arg-type]
    assert skip is None and profile is not None
    _git(ws, "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic ZHVtbXk=")
    _, skip, profile = _apply_fork_gate(_gate_cfg(), _provider("CONTRIBUTOR"))  # type: ignore[arg-type]
    assert isinstance(skip, SkipPlan) and skip.is_fork_gate_skip and profile is None
    assert "ZHVtbXk" not in skip.reason


def test_a_trusted_author_is_not_checked_for_a_persisted_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ws = _clean_repo(tmp_path)
    _git(ws, "config", "--local", "credential.helper", "store")
    monkeypatch.chdir(ws)
    monkeypatch.setattr(fork_gate, "persisted_credential_reason", _REAL_PERSISTED)
    _, skip, profile = _apply_fork_gate(_gate_cfg(), _provider("OWNER"))  # type: ignore[arg-type]
    assert skip is None and profile is None


# --- the runtime wiring ---------------------------------------------------------------------------


async def _build(provider: _P, tmp_path: Path, **cfg: Any) -> ReviewRuntime | SkipPlan:
    cfg.setdefault("analyzer_env_passthrough", ("FOO",))
    diff_result = _make_diff_result(changed_files=["a.js", "b.sh", "c.py"])
    with (
        patch("ai_pr_review.diff.compute.compute_diff", return_value=diff_result),
        patch("ai_pr_review.agents.gates.evaluate_gates", return_value={}),
        patch.dict("os.environ", {"AI_PR_REVIEW_DIFF_FILE": str(tmp_path / "diff.txt")}, clear=False),
    ):
        return await build_review_runtime(_gate_cfg(**cfg), provider_factory=lambda: provider)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize(("association", "is_forkrun"), [("CONTRIBUTOR", True), ("OWNER", False)])
async def test_passthrough_and_isolation_follow_the_fork_profile(
    anyio_backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, association: str, is_forkrun: bool,
) -> None:
    monkeypatch.setenv("FOO", "visible")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    result = await _build(_provider(association), tmp_path)
    assert isinstance(result, ReviewRuntime)
    env = _env.tool_env()
    assert (result.fork_profile is not None) is is_forkrun
    assert ("FOO" not in env) is is_forkrun and ("HTTPS_PROXY" not in env) is is_forkrun
    assert _env.fork_isolated() is is_forkrun


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_same_repository_run_keeps_the_passthrough(
    anyio_backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("FOO", "visible")
    result = await _build(_provider("NONE", head_repo="o/r"), tmp_path)
    assert isinstance(result, ReviewRuntime) and result.fork_profile is None
    assert _env.tool_env()["FOO"] == "visible" and not _env.fork_isolated()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_a_paused_fork_run_leaves_the_closed_state(
    anyio_backend: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("FOO", "visible")
    provider = _provider("CONTRIBUTOR")
    provider.facts = None  # unreadable facts pause the run
    result = await _build(provider, tmp_path)
    assert isinstance(result, SkipPlan) and "FOO" not in _env.tool_env() and _env.fork_isolated()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("association", ["CONTRIBUTOR", "OWNER"])
async def test_the_runtime_passes_the_fork_arguments_and_names_the_skipped_analyzers(
    anyio_backend: str, tmp_path: Path, association: str,
) -> None:
    fork = association == "CONTRIBUTOR"
    run_mock = AsyncMock(return_value=[])
    load_mock = MagicMock(return_value=[])
    with (
        patch("ai_pr_review.analyzers.bridge.run_analyzers", run_mock),
        patch("ai_pr_review.findings.suppress.load_rules", load_mock),
    ):
        result = await _build(_provider(association), tmp_path)
    assert isinstance(result, ReviewRuntime)
    assert run_mock.await_args.kwargs["fork_run"] is fork
    assert load_mock.call_args.kwargs["local_source"] == ("base-ref" if fork else "workspace")
    assert load_mock.call_args.kwargs["base_ref"] == "main"
    notice = result.not_reviewed_notice
    assert ("`eslint`" in notice and "`shellcheck`" in notice and "`ruff`" not in notice) is fork
    assert ("static analyzers" in notice) is fork


# --- the tool loop --------------------------------------------------------------------------------


def test_no_production_module_imports_the_tool_loop() -> None:
    """The tool loop is never enabled for a fork run, because nothing enables it. The story that
    wires it must refuse a fork profile and then change this test."""
    root = Path(__file__).resolve().parents[2] / "ai_pr_review"
    allowed = {root / "agents" / "tool_loop.py", root / "agents" / "tool_sandbox.py"}
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.endswith(("tool_loop", "tool_sandbox")) for n in names):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("http.extraheader", "AUTHORIZATION: basic ZHVtbXk="),
        ("url.https://user:pw@github.com/.insteadOf", "https://github.com/"),
    ],
)
def test_global_extraheader_and_credentialed_url_rewrite_pause(tmp_path: Path, key: str, value: str) -> None:
    ws = _repo(tmp_path / "ws")
    _git(ws, "config", "--local", key, value)
    reason = _REAL_PERSISTED(str(ws))
    assert reason
    assert "ZHVtbXk" not in reason
    assert "pw@" not in reason.split("(", 1)[-1] or "url." in reason


def test_phpcs_ignores_the_repo_ruleset_on_a_fork_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.analyzers import _env
    from ai_pr_review.analyzers.native import phpcs

    called: list[bool] = []
    monkeypatch.setattr(phpcs, "_installed_standards", lambda: {"PSR12": "/x"})
    monkeypatch.setattr(phpcs, "_find_repo_ruleset", lambda: called.append(True) or None)
    monkeypatch.setattr(_env, "_fork_isolated", True, raising=False)
    monkeypatch.setattr(phpcs, "fork_isolated", lambda: True)
    phpcs._plan_standard(["a.php"])
    assert called == []


def test_pause_text_hides_user_info_in_a_config_key(tmp_path: Path) -> None:
    ws = _repo(tmp_path / "ws")
    _git(ws, "config", "--local", "url.https://user:tok123@github.com/.insteadOf", "https://github.com/")
    reason = _REAL_PERSISTED(str(ws))
    assert reason
    assert "tok123" not in reason
    assert "url.<url>.insteadof" in reason.lower()
