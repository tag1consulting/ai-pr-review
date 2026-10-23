"""Tests for the native phpstan analyzer."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.phpstan import _run_phpstan, _severity_for_level
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "phpstan"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(php_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=php_files, php=php_files)


class TestSeverityForLevel:
    def test_level_0_to_4_maps_to_low(self) -> None:
        for level in ("0", "1", "2", "3", "4"):
            assert _severity_for_level(level) == "Low", f"level {level} should be Low"

    def test_level_5_to_7_maps_to_medium(self) -> None:
        for level in ("5", "6", "7"):
            assert _severity_for_level(level) == "Medium", f"level {level} should be Medium"

    def test_level_8_to_9_maps_to_high(self) -> None:
        for level in ("8", "9"):
            assert _severity_for_level(level) == "High", f"level {level} should be High"


class TestRunPhpstanGuards:
    def test_no_php_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_phpstan(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value=None):
            result = _run_phpstan(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_phpstan(cf, Path("/dev/null"))
        assert "phpstan not found" in caplog.text

    def test_invalid_level_falls_back_to_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        monkeypatch.setenv("PHPSTAN_LEVEL", "bogus")
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--level=3" in call_args

    def test_valid_level_env_var_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        monkeypatch.setenv("PHPSTAN_LEVEL", "7")
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--level=7" in call_args

    def test_double_dash_precedes_target_files(self, tmp_path: Path) -> None:
        # Argument-injection guard: every target file must follow a literal
        # "--" so a dash-leading filename cannot be parsed as a phpstan flag.
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--" in call_args
        assert call_args.index("--") < call_args.index(str(f))


class TestRunPhpstanFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            return _run_phpstan(cf, Path("/dev/null"))

    def test_error_finding_at_default_level(self, tmp_path: Path) -> None:
        # Default level is 3, which maps to Low.
        findings = self._run_with_fixture("phpstan-error.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Low"
        assert f.confidence == 85
        assert f.source == "phpstan"
        assert "getTitle" in f.finding
        assert f.line == 55

    def test_severity_follows_phpstan_level(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fixture = _load_fixture("phpstan-error.json")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        for level, expected in (("3", "Low"), ("6", "Medium"), ("9", "High")):
            monkeypatch.setenv("PHPSTAN_LEVEL", level)
            with (
                patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
                patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
                findings = _run_phpstan(cf, Path("/dev/null"))
            assert findings[0].severity == expected, f"level {level} should yield {expected}"

    def test_empty_files_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpstan-empty.json", tmp_path)
        assert findings == []

    def test_category_is_lint(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpstan-error.json", tmp_path)
        assert findings[0].category == "lint"

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("phpstan-malformed.json")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            result = _run_phpstan(cf, Path("/dev/null"))
        assert result == []
        assert "non-JSON" in caplog.text

    def test_cwd_prefix_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # strip_workspace_prefix() prefers GITHUB_WORKSPACE when set (#846);
        # real CI runners always set it, so this cwd-fallback case must
        # explicitly unset it, not just rely on it being absent locally.
        monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
        cwd = os.getcwd()
        payload = json.dumps({
            "totals": {"errors": 1, "file_errors": 1},
            "files": {
                f"{cwd}/src/MyService.php": {
                    "errors": 1,
                    "messages": [{"message": "Type error", "line": 10, "ignorable": True}],
                }
            },
            "errors": [],
        })
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings[0].file == "src/MyService.php"

    def test_github_workspace_prefix_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#846: phpstan's own cwd-only stripping leaked an absolute path
        whenever cwd != GITHUB_WORKSPACE. Now consolidated onto
        _paths.strip_workspace_prefix, which prefers GITHUB_WORKSPACE."""
        monkeypatch.setenv("GITHUB_WORKSPACE", "/some/container/workspace")
        payload = json.dumps({
            "totals": {"errors": 1, "file_errors": 1},
            "files": {
                "/some/container/workspace/src/MyService.php": {
                    "errors": 1,
                    "messages": [{"message": "Type error", "line": 10, "ignorable": True}],
                }
            },
            "errors": [],
        })
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings[0].file == "src/MyService.php"

    def test_exitcode_2_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="config error")
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="phpstan", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run", side_effect=OSError("not found")),
        ):
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert findings == []

    def test_workspace_phpstan_neon_never_auto_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork-committed phpstan.neon must not suppress --level or be
        passed to phpstan; the analyzer always drives its own trusted,
        empty --configuration file instead (#739)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "phpstan.neon").write_text("parameters:\n  level: 5\n  bootstrapFiles: [evil.php]\n")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--level=3" in call_args
        config_args = [a for a in call_args if a.startswith("--configuration=")]
        assert len(config_args) == 1
        configured_path = config_args[0].removeprefix("--configuration=")
        assert configured_path != str(tmp_path / "phpstan.neon")
        assert not configured_path.startswith(str(tmp_path))

    def test_workspace_vendor_autoload_not_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fork-committed vendor/autoload.php must never be passed to
        --autoload-file -- only the image-baked, absolute composer path
        counts (#739)."""
        monkeypatch.chdir(tmp_path)
        vendor_drupal = tmp_path / "vendor" / "mglaman" / "phpstan-drupal"
        vendor_drupal.mkdir(parents=True)
        (tmp_path / "vendor" / "autoload.php").write_text("<?php\n")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert not any(a.startswith("--autoload-file=") for a in call_args)

    def test_baked_drupal_autoload_used_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The image-baked composer path (absolute, never fork-controlled)
        is honored when present."""
        baked_vendor = tmp_path / "opt-composer" / "vendor"
        (baked_vendor / "mglaman" / "phpstan-drupal").mkdir(parents=True)
        (baked_vendor / "autoload.php").write_text("<?php\n")
        monkeypatch.setattr(
            "ai_pr_review.analyzers.native.phpstan._BAKED_PHPSTAN_DRUPAL",
            baked_vendor / "mglaman" / "phpstan-drupal",
        )
        monkeypatch.setattr(
            "ai_pr_review.analyzers.native.phpstan._BAKED_COMPOSER_AUTOLOAD",
            baked_vendor / "autoload.php",
        )
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert f"--autoload-file={baked_vendor / 'autoload.php'}" in call_args

    def test_trusted_config_file_cleaned_up(self, tmp_path: Path) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        configured_path = next(a for a in call_args if a.startswith("--configuration=")).removeprefix(
            "--configuration="
        )
        assert not Path(configured_path).exists()

    def test_unresolved_symbol_messages_dropped(self, tmp_path: Path) -> None:
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        out = json.dumps({"files": {str(f): {"errors": 4, "messages": [
            {"message": "Instantiated class App\\Other not found.", "line": 3, "identifier": "class.notFound"},
            {"message": "Function helper_fn not found.", "line": 3, "identifier": "function.notFound"},
            {"message": "Call to method f() on an unknown class App\\Other.", "line": 3},
            {"message": "Class A implements unknown interface B.", "line": 3, "identifier": "interface.notFound"},
            {"message": "Class A uses unknown trait T.", "line": 3, "identifier": "trait.notFound"},
            {"message": "Attribute class X does not exist.", "line": 3, "identifier": "attribute.notFound"},
            {"message": "Class A uses unknown trait T.", "line": 3},
            {"message": "Method A::g() should return int but returns string.", "line": 5, "identifier": "return.type"},
            {"message": "Call to an undefined method A::h().", "line": 6, "identifier": "method.notFound"},
            {"message": "Offset 'x' does not exist on array. Value not found.", "line": 7, "identifier": "offsetAccess.notFound"},
        ]}}, "errors": []})
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=out, stderr="")
            findings = _run_phpstan(cf, Path("/dev/null"))
        assert [x.line for x in findings] == [5, 6, 7]

    def test_php_runtime_and_version_manager_env_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_home = tmp_path / "home"
        (real_home / ".asdf").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.setenv("PHPRC", "/etc/php")
        monkeypatch.setenv("ASDF_DIR", "/opt/asdf")
        monkeypatch.setenv("MISE_DATA_DIR", "/opt/mise")
        monkeypatch.setenv("MISE_GITHUB_TOKEN", "x")
        monkeypatch.delenv("ASDF_DATA_DIR", raising=False)
        monkeypatch.setenv("SECRET_TOKEN", "x")
        f = tmp_path / "MyService.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")
            _run_phpstan(cf, Path("/dev/null"))
        env = mock_run.call_args.kwargs["env"]
        assert env["PHPRC"] == "/etc/php"
        assert env["ASDF_DIR"] == "/opt/asdf"
        assert env["MISE_DATA_DIR"] == "/opt/mise"
        assert "MISE_GITHUB_TOKEN" not in env
        assert env["ASDF_DATA_DIR"] == str(real_home / ".asdf")
        assert env["HOME"] != str(real_home)
        assert "SECRET_TOKEN" not in env

    def test_remediation_references_phpstan(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpstan-error.json", tmp_path)
        assert "phpstan.org" in findings[0].remediation


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_phpstan_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["MyService.php"], php=["MyService.php"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "phpstan" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_phpstan_skipped_when_no_php_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("phpstan", ["php"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called


class TestWorkspaceIsolation:
    def test_run_is_isolated_from_the_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_TOKEN", "secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        (tmp_path / "a.php").write_text("<?php\n")
        cf = _make_cf(["a.php"])
        seen: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            run_cwd = Path(str(kwargs["cwd"]))
            seen["entries"] = sorted(p.name for p in run_cwd.iterdir())
            return MagicMock(returncode=0, stdout='{"files":{},"errors":[]}', stderr="")

        with (
            patch("ai_pr_review.analyzers.native.phpstan.shutil.which", return_value="/usr/bin/phpstan"),
            patch("ai_pr_review.analyzers.native.phpstan.subprocess.run", side_effect=fake_run),
        ):
            _run_phpstan(cf, Path("/dev/null"))

        kwargs = seen["kwargs"]
        assert isinstance(kwargs, dict)
        run_cwd = Path(str(kwargs["cwd"]))
        assert not run_cwd.resolve().is_relative_to(tmp_path.resolve())
        assert seen["entries"] == ["phpstan-trusted.neon"]
        assert not run_cwd.exists()
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["HOME"] == str(run_cwd)
        assert "GITHUB_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
        assert env["HTTPS_PROXY"] == "http://proxy.example:3128"
        cmd = seen["cmd"]
        assert isinstance(cmd, list)
        assert cmd[-1] == str(tmp_path / "a.php")


@pytest.mark.skipif(shutil.which("phpstan") is None, reason="phpstan is not installed")
class TestRealPhpstanHostileTree:
    def test_workspace_autoload_and_config_do_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marks = tmp_path / "marks"
        marks.mkdir()
        ws = tmp_path / "ws"
        (ws / "vendor").mkdir(parents=True)
        (ws / "a.php").write_text("<?php\nfunction f(): int { return 'text'; }\n")
        (ws / "vendor" / "autoload.php").write_text(
            f"<?php file_put_contents('{marks}/autoload', 'ran');\n"
        )
        (ws / "boot.php").write_text(f"<?php file_put_contents('{marks}/boot', 'ran');\n")
        (ws / "phpstan.neon").write_text("parameters:\n    bootstrapFiles:\n        - boot.php\n")
        monkeypatch.chdir(ws)
        findings = _run_phpstan(_make_cf(["a.php"]), Path("/dev/null"))
        assert list(marks.iterdir()) == []
        assert findings, "phpstan did not report the return type error"
