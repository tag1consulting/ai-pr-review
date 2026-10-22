"""Story 17-8: every analyzer, git, rg and gh launch gets a scrubbed environment."""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers import _env
from ai_pr_review.analyzers._env import gh_env, git_env, tool_env
from ai_pr_review.analyzers.bridge import _ANALYZERS
from ai_pr_review.manifest import ChangedFiles

_ROOT = Path(__file__).resolve().parents[2] / "ai_pr_review"
_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "OPENAI_API_KEY": "sk-openai-secret",
    "GH_TOKEN": "ghp_secret",
    "GITHUB_TOKEN": "ghs_secret",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret",
}


@pytest.fixture(autouse=True)
def _reset_env_state() -> Any:
    _env.configure_passthrough((), allow_passthrough=True)
    _env.configure_fork_isolation(False)
    yield
    _env.configure_passthrough((), allow_passthrough=True)
    _env.configure_fork_isolation(False)


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    return _SECRETS


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: Any, *a: Any, **kw: Any) -> Any:
        self.calls.append((list(args), kw))
        return MagicMock(returncode=0, stdout="", stderr="", args=args)


def _assert_scrubbed(rec: _Recorder, secrets: dict[str, str], *, allow: tuple[str, ...] = ()) -> None:
    assert rec.calls, "no launch was captured"
    for argv, kw in rec.calls:
        env = kw.get("env")
        assert env is not None, f"launch without env: {argv[:3]}"
        for name, value in secrets.items():
            if name in allow:
                continue
            assert name not in env, f"{name} leaked into {argv[:3]}"
            assert value not in env.values()


# --- helper unit tests ---------------------------------------------------------------


def test_tool_env_allowlist_only(secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_OTHER", "x")
    for name in _env._NETWORK_NAMES:
        monkeypatch.delenv(name, raising=False)
    env = tool_env()
    assert set(env) == {"PATH", "HOME", "LC_ALL", "PYTHONSAFEPATH"}
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["PYTHONSAFEPATH"] == "1"


def test_home_is_one_directory_per_run() -> None:
    assert tool_env()["HOME"] == tool_env()["HOME"] == git_env()["HOME"]
    assert Path(tool_env()["HOME"]).is_dir()


def test_extra_reaches_env() -> None:
    assert tool_env(extra={"TFLINT_PLUGIN_DIR": "/x"})["TFLINT_PLUGIN_DIR"] == "/x"


def test_passthrough_normal_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_SETTING", "custom")
    _env.configure_passthrough(("TOOL_SETTING",))
    assert tool_env()["TOOL_SETTING"] == "custom"
    assert git_env()["TOOL_SETTING"] == "custom"


def test_passthrough_ignored_when_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_SETTING", "custom")
    _env.configure_passthrough(("TOOL_SETTING",), allow_passthrough=False)
    assert "TOOL_SETTING" not in tool_env()
    assert "TOOL_SETTING" not in tool_env(passthrough=("TOOL_SETTING",), allow_passthrough=False)


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "ACTIONS_X"])
def test_passthrough_refuses_secret_names(
    name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(name, "secret-value")
    _env.configure_passthrough((name,))
    with caplog.at_level("WARNING"):
        env = tool_env()
    assert name not in env
    assert name in caplog.text
    assert "secret-value" not in caplog.text


def test_gh_env_keeps_token_and_nothing_else_secret(secrets: dict[str, str]) -> None:
    env = gh_env()
    assert env["GH_TOKEN"] == "ghp_secret"
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        assert name not in env


def test_config_parses_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig

    cfg = ReviewConfig.model_validate({"analyzer_env_passthrough": ("A", "B")})
    assert cfg.analyzer_env_passthrough == ("A", "B")
    assert ReviewConfig.model_validate({}).analyzer_env_passthrough == ()


# --- hostile capture: analyzers -------------------------------------------------------


def _tree(tmp_path: Path) -> ChangedFiles:
    files = {
        "a.py": "x = 1\n",
        "main.go": "package main\n",
        "go.mod": "module x\n",
        "a.php": "<?php\n",
        "main.tf": 'resource "x" "y" {}\n',
        "Dockerfile": "FROM scratch\n",
        "a.js": "var x;\n",
        "k8s.yaml": "kind: Pod\n",
        "a.sh": "echo hi\n",
        "README.md": "see old.md\n",
    }
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    paths = {n: str(tmp_path / n) for n in files}
    return ChangedFiles(
        all_files=list(paths.values()),
        source=[paths["a.py"], paths["a.php"], paths["main.go"], paths["a.js"]],
        docs=[paths["README.md"]],
        python=[paths["a.py"]],
        go=[paths["main.go"]],
        php=[paths["a.php"]],
        terraform=[paths["main.tf"]],
        dockerfile=[paths["Dockerfile"]],
        iac=[paths["k8s.yaml"], paths["main.tf"]],
        js_ts=[paths["a.js"]],
        shell=[paths["a.sh"]],
    )


def test_every_analyzer_launch_is_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets: dict[str, str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cf = _tree(tmp_path)
    diff = tmp_path / "d.diff"
    diff.write_text(
        "diff --git a/old.md b/old.md\ndeleted file mode 100644\n--- a/old.md\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
    )
    rec = _Recorder()
    with (
        patch("subprocess.run", rec),
        patch("shutil.which", return_value="/usr/bin/tool"),
        patch("ai_pr_review.analyzers.native.cve_check.httpx", create=True),
    ):
        for spec in _ANALYZERS:
            if spec.name in ("cve-check", "dep-exists"):
                continue  # no subprocess
            with contextlib.suppress(Exception):
                spec.native_fn(cf, diff)
    launched = {argv[0] for argv, _ in rec.calls}
    assert {"tflint", "ruff", "trufflehog", "semgrep", "phpcs", "phpstan", "checkov"} <= launched
    _assert_scrubbed(rec, secrets)


def test_tflint_keeps_plugin_dir_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets: dict[str, str]) -> None:
    from ai_pr_review.analyzers.native.tflint import _run_tflint

    monkeypatch.chdir(tmp_path)
    tf = tmp_path / "main.tf"
    tf.write_text("\n")
    rec = _Recorder()
    with patch("subprocess.run", rec), patch("shutil.which", return_value="/usr/bin/tflint"):
        _run_tflint(ChangedFiles(all_files=[str(tf)], terraform=[str(tf)]), Path("/dev/null"))
    assert "TFLINT_PLUGIN_DIR" in rec.calls[0][1]["env"]
    _assert_scrubbed(rec, secrets)


def test_golangci_lint_gocache_under_temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets: dict[str, str]
) -> None:
    from ai_pr_review.analyzers.native.golangci_lint import _run_golangci_lint

    monkeypatch.chdir(tmp_path)
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "main.go").write_text("package main\n")
    rec = _Recorder()
    with patch("subprocess.run", rec), patch("shutil.which", return_value="/usr/bin/g"):
        _run_golangci_lint(ChangedFiles(all_files=["main.go"], go=["main.go"]), Path("/dev/null"))
    env = rec.calls[0][1]["env"]
    assert env["GOCACHE"].startswith(env["HOME"])
    _assert_scrubbed(rec, secrets)


def test_passthrough_reaches_analyzer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.analyzers.native.trufflehog import _run_trufflehog

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOOL_SETTING", "custom")
    (tmp_path / "a.txt").write_text("x\n")
    _env.configure_passthrough(("TOOL_SETTING",))
    rec = _Recorder()
    with patch("subprocess.run", rec), patch("shutil.which", return_value="/usr/bin/t"):
        _run_trufflehog(ChangedFiles(all_files=["a.txt"]), Path("/dev/null"))
    assert rec.calls[0][1]["env"]["TOOL_SETTING"] == "custom"


def test_ripgrep_symbol_lookup_is_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets: dict[str, str]
) -> None:
    from ai_pr_review.context import symbols

    rec = _Recorder()
    with patch("subprocess.run", rec), patch("shutil.which", return_value="/usr/bin/rg"):
        symbols.lookup_definitions(
            [symbols.SymbolRef(name="foo", kind="identifier", line=1)], tmp_path, ["a.py"], "Python"
        )
    assert rec.calls[0][0][0] == "/usr/bin/rg"
    _assert_scrubbed(rec, secrets)


# --- hostile capture: git and gh ---------------------------------------------------


def test_compute_diff_git_launches_are_scrubbed(secrets: dict[str, str]) -> None:
    from ai_pr_review.diff.compute import compute_diff

    rec = _Recorder()
    with patch("subprocess.run", rec), contextlib.suppress(Exception):
        compute_diff("main", "a" * 40, last_reviewed_sha="b" * 40, workspace="/ws")
    assert len(rec.calls) >= 3
    _assert_scrubbed(rec, secrets)
    for argv, _ in rec.calls:
        assert argv[:5] == ["git", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=*"]
        assert not any(a == "GIT_CONFIG_NOSYSTEM" for a in argv)


def test_filtered_diff_launches_are_scrubbed(secrets: dict[str, str]) -> None:
    from ai_pr_review.diff.compute import _filtered_diff

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: Any, *a: Any, **kw: Any) -> Any:
        calls.append((list(args), kw))
        out = "c" * 40 + "\n" if "rev-list" in args or "rev-parse" in args else ""
        return MagicMock(returncode=0, stdout=out, stderr="")

    with patch("subprocess.run", run), contextlib.suppress(Exception):
        _filtered_diff("main", "d" * 40, "e" * 40, workspace="/ws")
    assert len(calls) >= 6
    for _argv, kw in calls:
        assert kw.get("env") is not None
        assert not (set(_SECRETS) & set(kw["env"]))


def test_policy_git_show_is_scrubbed(secrets: dict[str, str]) -> None:
    from ai_pr_review import policy

    rec = _Recorder()
    with patch("subprocess.run", rec):
        policy._git_show_policy_path("/ws", "main", ".ai-pr-review/policy.yml")
    _assert_scrubbed(rec, secrets)
    assert rec.calls[0][0][:5] == ["git", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=*"]


def test_preflight_gh_keeps_token_only(secrets: dict[str, str]) -> None:
    from ai_pr_review.review import preflight

    rec = _Recorder()
    with patch("subprocess.run", rec):
        preflight.fetch_open_issues("o/r")
    env = rec.calls[0][1]["env"]
    assert env["GH_TOKEN"] == "ghp_secret"
    _assert_scrubbed(rec, secrets, allow=("GH_TOKEN",))


def test_preflight_git_launches_are_scrubbed(tmp_path: Path, secrets: dict[str, str]) -> None:
    import inspect

    from ai_pr_review.review import preflight

    src = inspect.getsource(preflight)
    assert src.count("env=git_env()") >= 3
    assert src.count("*GIT_SAFE_ARGS") >= 3


# --- grep-style guard ---------------------------------------------------------------

_EXEMPT = {"agents/tool_sandbox.py", "review/fork_gate.py"}


def test_every_subprocess_run_passes_env() -> None:
    missing: list[str] = []
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _EXEMPT:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"run", "Popen", "check_output", "check_call", "call"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and (
                    not any(kw.arg == "env" for kw in node.keywords)
                    or any(
                        kw.arg == "env" and "os.environ" in ast.unparse(kw.value)
                        for kw in node.keywords
                    )
                )
            ):
                missing.append(f"{rel}:{node.lineno}")
    assert not missing, f"subprocess launch without env=: {missing}"


def test_run_review_configures_passthrough() -> None:
    import inspect

    from ai_pr_review.review import runtime

    assert "configure_passthrough(config.analyzer_env_passthrough" in inspect.getsource(runtime)


def test_gh_env_uses_its_own_home_apart_from_the_analyzer_home(monkeypatch) -> None:
    from ai_pr_review.analyzers import _env

    monkeypatch.setenv("GH_TOKEN", "tok")
    env = _env.gh_env()
    assert env["HOME"] != _env.run_home()
    assert env["GH_CONFIG_DIR"] == env["HOME"]
    assert env["GH_TOKEN"] == "tok"


@pytest.mark.parametrize(
    "name", ["GITLAB_TOKEN", "CI_JOB_TOKEN", "BITBUCKET_API_TOKEN", "AWS_ACCESS_KEY_ID", "MY_SECRET"]
)
def test_credential_looking_names_are_refused(name: str) -> None:
    from ai_pr_review.analyzers import _env

    assert _env.is_blocked_name(name)


def test_proxy_and_ca_settings_reach_every_launch_and_gh_keeps_host(monkeypatch) -> None:
    from ai_pr_review.analyzers import _env

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ca.pem")
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    assert _env.tool_env()["HTTPS_PROXY"] == "http://proxy:3128"
    assert _env.tool_env()["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert "GH_HOST" not in _env.tool_env()
    assert _env.gh_env()["GH_HOST"] == "ghe.example.com"


def test_proxy_with_credentials_is_not_forwarded_and_fork_runs_get_no_network_settings(
    monkeypatch,
) -> None:
    from ai_pr_review.analyzers import _env

    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert "HTTPS_PROXY" not in _env.tool_env()
    assert _env.tool_env()["NO_PROXY"] == "localhost"
    assert "NO_PROXY" not in _env.tool_env(allow_passthrough=False)


def test_schemeless_proxy_with_credentials_is_not_forwarded(monkeypatch) -> None:
    from ai_pr_review.analyzers import _env

    monkeypatch.setenv("HTTPS_PROXY", "user:pw@proxy:3128")
    monkeypatch.setenv("http_proxy", "user@proxy:3128")
    assert "HTTPS_PROXY" not in _env.tool_env()
    assert "http_proxy" not in _env.tool_env()


def test_a_credentialed_proxy_named_in_the_passthrough_list_is_refused(monkeypatch) -> None:
    from ai_pr_review.analyzers import _env

    monkeypatch.setenv("ALL_PROXY", "http://user:pw@proxy:3128")
    monkeypatch.setenv("FTP_PROXY", "http://proxy:3128")
    env = _env.tool_env(passthrough=("ALL_PROXY", "FTP_PROXY"), allow_passthrough=True)
    assert "ALL_PROXY" not in env
    assert env["FTP_PROXY"] == "http://proxy:3128"
