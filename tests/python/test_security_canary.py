"""Security canary tests: verify the action never executes checked-out workspace files.

Each test exercises a code path that reads files from $GITHUB_WORKSPACE as data and
asserts that none of the sentinel files in CANARY_DIR were written. A sentinel file
appearing means the code path *executed* a workspace file rather than just reading it.

These tests are marked @pytest.mark.security. A failure is a security regression, not
a flaky test — see SECURITY.md and CONTRIBUTING.md for the reporting and remediation
process.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CANARY_WORKSPACE = Path(__file__).parent.parent / "security" / "canary-workspace"
REPO_ROOT = Path(__file__).parent.parent.parent

SENTINEL_NAMES = [
    "makefile-executed",
    "setup-py-imported",
    "npm-preinstall-executed",
    "npm-postinstall-executed",
    "pre-commit-hook-executed",
    "conftest-imported",
    "semgrep-rule-executed",
    "phpcs-ruleset-executed",
    "phpcs-hostile-ref-executed",
]


def _assert_no_canary_fired(canary_dir: Path) -> None:
    """Assert no sentinel file was written under canary_dir."""
    if not canary_dir.exists():
        return
    fired = [f.name for f in canary_dir.iterdir() if f.name in SENTINEL_NAMES]
    assert not fired, (
        f"SECURITY REGRESSION: workspace execution sentinels triggered: {fired}. "
        "The action executed checked-out code from the workspace, violating the "
        "'never execute checked-out code' invariant. "
        "See SECURITY.md and CONTRIBUTING.md for the reporting process."
    )


@pytest.mark.security
class TestNeverExecuteCheckedOutCode:
    """Verify each workspace-reading code path treats files as data, never as code."""

    def test_language_detection_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """languages.py detects language by extension/basename — must not exec any file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.languages import detect_language, is_test_file

        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file():
                detect_language(f.suffix.lstrip("."))
                is_test_file(str(f))

        _assert_no_canary_fired(canary_dir)

    def test_manifest_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """manifest.py classifies files by path/extension — must not exec any file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.manifest import build_changed_files

        file_list = [
            str(f.relative_to(CANARY_WORKSPACE))
            for f in CANARY_WORKSPACE.iterdir()
            if f.is_file()
        ]
        result = build_changed_files(file_list)
        assert result is not None

        _assert_no_canary_fired(canary_dir)

    def test_analyzer_bridge_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """analyzer bridge runs regex/AST analysis on file content — must not exec files."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.analyzers.bridge import run_analyzers
        from ai_pr_review.manifest import build_changed_files

        file_list = [
            str(f.relative_to(CANARY_WORKSPACE))
            for f in CANARY_WORKSPACE.iterdir()
            if f.is_file()
        ]
        changed_files = build_changed_files(file_list)

        # Write a minimal diff file so the analyzer has something to read as data.
        diff_file = str(tmp_path / "test.diff")
        Path(diff_file).write_text("", encoding="utf-8")

        # Only suppress expected infrastructure errors (missing deps, missing files).
        # Unexpected exceptions — including anything raised during workspace execution —
        # must not be swallowed, so the test fails rather than passing vacuously.
        with contextlib.suppress(ImportError, FileNotFoundError, OSError):
            asyncio.run(
                run_analyzers(changed_files=changed_files, diff_file=diff_file)
            )

        _assert_no_canary_fired(canary_dir)

    def test_sarif_ingestor_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SARIF ingestor reads JSON as data — must not execute any workspace file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.analyzers.sarif import load_sarif_files

        # load_sarif_files is fail-soft — a missing path returns [] and logs a warning.
        findings, _elapsed = load_sarif_files(["nonexistent.sarif"])
        assert findings == []

        _assert_no_canary_fired(canary_dir)

    def test_context_enrichment_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tree-sitter parses file content as AST — must not exec any workspace file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))

        try:
            from ai_pr_review.context.treesitter import extract_symbol_refs
        except ImportError:
            pytest.skip("tree-sitter-language-pack not installed")

        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file() and f.suffix == ".py":
                # Pass file content as a diff hunk string — purely data, never executed.
                hunk = f.read_text(errors="replace")
                extract_symbol_refs(hunk, language="python")

        _assert_no_canary_fired(canary_dir)

    def test_language_profile_loading_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """language_profiles.py reads markdown from the image install — not workspace."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.language_profiles import load_language_profiles

        # Point script_dir at a temp dir with no language-profiles/ subdir — the
        # function is fail-soft and returns "" when the directory doesn't exist.
        # This verifies it never falls back to reading from CANARY_WORKSPACE.
        result = load_language_profiles(["Python", "JavaScript"], script_dir=tmp_path)
        assert isinstance(result, str)

        # Also verify with a real script_dir that has profiles (if available).
        # Either way, no workspace files should be executed.
        import ai_pr_review

        real_script_dir = Path(ai_pr_review.__file__).parent
        load_language_profiles(["Python"], script_dir=real_script_dir)

        _assert_no_canary_fired(canary_dir)

    def test_reading_workspace_files_as_bytes_does_not_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading canary files as raw bytes (simulating diff content reads) fires no sentinel."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))

        # Simulate what diff/compute.py does: open and read file bytes for diffing.
        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file():
                _ = f.read_bytes()

        _assert_no_canary_fired(canary_dir)


@pytest.mark.security
class TestPhpcsRulesetIsData:
    """The phpcs analyzer reads a repo ruleset as data and never hands it to phpcs (#1042)."""

    def test_phpcs_never_receives_the_workspace_ruleset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess
        from unittest.mock import MagicMock, patch

        from ai_pr_review.analyzers.native.phpcs import _run_phpcs
        from ai_pr_review.manifest import ChangedFiles

        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        monkeypatch.chdir(CANARY_WORKSPACE)
        seen: list[list[str]] = []
        generated: list[str] = []

        def _fake(args: list[str], **kwargs: object) -> MagicMock:
            seen.append(args)
            if args[0:2] == ["phpcs", "-i"]:
                return MagicMock(returncode=0, stdout="PSR12, PSR2, PSR1", stderr="")
            flag = next(a for a in args if a.startswith("--standard="))
            generated.append(Path(flag.removeprefix("--standard=")).read_text())
            return MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")

        files = ["canary-bootstrap.php"]
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch.object(subprocess, "run", side_effect=_fake),
        ):
            _run_phpcs(ChangedFiles(all_files=files, php=files), tmp_path / "x.diff")

        main = seen[-1]
        standard = next(a for a in main if a.startswith("--standard="))
        assert "phpcs.xml.dist" not in standard
        assert str(CANARY_WORKSPACE) not in standard
        assert generated, "phpcs should receive a generated ruleset"
        for needle in ("installed_paths", "php_path", "bootstrap", "autoload", "canary-bootstrap", "Generic.canary"):
            assert needle not in generated[-1], needle
        _assert_no_canary_fired(canary_dir)

    def test_real_phpcs_does_not_load_workspace_php(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        if not (shutil.which("phpcs") and shutil.which("php")):
            pytest.skip("phpcs and php are not installed")
        from ai_pr_review.analyzers.native.phpcs import _run_phpcs
        from ai_pr_review.manifest import ChangedFiles

        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        monkeypatch.chdir(CANARY_WORKSPACE)
        files = ["canary-bootstrap.php"]
        _run_phpcs(ChangedFiles(all_files=files, php=files), tmp_path / "x.diff")
        _assert_no_canary_fired(canary_dir)

    def test_real_phpcs_with_ref_shapes_that_resolve_against_the_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule ref or exclude name that names a workspace file or directory must not load it (#1042)."""
        import shutil

        if not (shutil.which("phpcs") and shutil.which("php")):
            pytest.skip("phpcs and php are not installed")
        from ai_pr_review.analyzers.native.phpcs import _plan_standard

        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        monkeypatch.chdir(CANARY_WORKSPACE)
        plan = _plan_standard(["canary-bootstrap.php"])
        assert plan is not None
        assert plan.ruleset_xml is not None
        assert "Generic.canary" not in plan.ruleset_xml
        _assert_no_canary_fired(canary_dir)


    def test_symlinked_changed_file_is_never_handed_to_phpcs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A changed file that is a symlink to a file outside the workspace must be skipped (#1047)."""
        import subprocess
        from unittest.mock import MagicMock, patch

        from ai_pr_review.analyzers.native.phpcs import _run_phpcs
        from ai_pr_review.manifest import ChangedFiles

        link = CANARY_WORKSPACE / "canary-link.php"
        assert link.is_symlink(), "the canary symlink is missing from the workspace"
        assert not (CANARY_WORKSPACE / link.resolve().name).exists()

        monkeypatch.setenv("CANARY_DIR", str(tmp_path / "canary"))
        monkeypatch.chdir(CANARY_WORKSPACE)
        calls: list[list[str]] = []

        def _fake(args: list[str], **kwargs: object) -> MagicMock:
            calls.append(args)
            return MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")

        files = ["canary-link.php"]
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch.object(subprocess, "run", side_effect=_fake),
        ):
            findings = _run_phpcs(ChangedFiles(all_files=files, php=files), tmp_path / "x.diff")

        assert findings == []
        assert calls == [], "phpcs must not run when the only changed file escapes the workspace"

    def test_real_phpcs_with_hostile_ruleset_does_not_read_outside_the_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the real phpcs against a hostile ruleset and check that nothing outside the workspace loads (#1047).

        The ruleset uses path-like refs, a ref that exists relative to the
        working directory, an absolute ref, and an out-of-tree `<file>`. A
        control run hands the same ruleset to phpcs directly. The control must
        fire the sentinel, or the hostile input proves nothing.
        """
        import shutil
        import subprocess

        if not (shutil.which("phpcs") and shutil.which("php")):
            pytest.skip("the real phpcs and php binaries are not installed")
        from ai_pr_review.analyzers.native.phpcs import _run_phpcs
        from ai_pr_review.manifest import ChangedFiles

        outside = tmp_path / "outside"
        workspace = tmp_path / "ws"
        outside.mkdir()
        workspace.mkdir()
        sentinel_dir = tmp_path / "sentinels"

        def _bootstrap(directory: Path) -> Path:
            php = directory / "boot.php"
            php.write_text(
                "<?php\n$d = getenv('CANARY_DIR');\n@mkdir($d, 0777, true);\n"
                "@touch($d . '/phpcs-hostile-ref-executed');\n",
                encoding="utf-8",
            )
            return php

        def _ruleset(path: Path, boot: Path) -> None:
            path.write_text(
                f'<?xml version="1.0"?>\n<ruleset name="hostile"><arg name="bootstrap" value="{boot}"/>'
                '<rule ref="Generic.PHP.Syntax"/></ruleset>\n',
                encoding="utf-8",
            )

        outside_boot = _bootstrap(outside)
        _ruleset(outside / "outside-ruleset.xml", outside_boot)
        (outside / "secret.php").write_text("<?php\n// OUTSIDE-SECRET-MARKER\n$x = 1 ;\n", encoding="utf-8")

        workspace_boot = _bootstrap(workspace)
        _ruleset(workspace / "Generic.hostile.xml", workspace_boot)
        (workspace / "hostile").mkdir()
        _ruleset(workspace / "hostile" / "ruleset.xml", workspace_boot)
        (workspace / "linked.php").symlink_to(outside / "secret.php")
        (workspace / "changed.php").write_text("<?php\n$x = 1 ;\n", encoding="utf-8")
        hostile = workspace / "phpcs.xml"
        hostile.write_text(
            f"""<?xml version="1.0"?>
<ruleset name="hostile">
  <config name="installed_paths" value="{outside}"/>
  <arg name="bootstrap" value="{workspace_boot}"/>
  <autoload>{workspace_boot}</autoload>
  <file>{outside}</file>
  <file>../outside</file>
  <rule ref="{outside}/outside-ruleset.xml"/>
  <rule ref="../outside/outside-ruleset.xml"/>
  <rule ref="./Generic.hostile.xml"/>
  <rule ref="Generic.hostile.xml"/>
  <rule ref="hostile"/>
  <rule ref="Generic.PHP.Syntax">
    <exclude name="hostile"/>
    <exclude name="Generic.hostile.xml"/>
  </rule>
  <rule ref="PSR12"/>
</ruleset>
""",
            encoding="utf-8",
        )

        monkeypatch.chdir(workspace)

        control_dir = tmp_path / "control"
        monkeypatch.setenv("CANARY_DIR", str(control_dir))
        subprocess.run(
            ["phpcs", "--report=json", f"--standard={outside}/outside-ruleset.xml", "-q", "changed.php"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert (control_dir / "phpcs-hostile-ref-executed").exists(), (
            "control failed: the real phpcs did not run the hostile bootstrap, so this test proves nothing"
        )

        monkeypatch.setenv("CANARY_DIR", str(sentinel_dir))
        files = ["changed.php", "linked.php"]
        findings = _run_phpcs(ChangedFiles(all_files=files, php=files), tmp_path / "x.diff")

        _assert_no_canary_fired(sentinel_dir)
        assert findings, "phpcs should still lint changed.php, or the run proves nothing"
        assert not sentinel_dir.exists() or not list(sentinel_dir.iterdir())
        assert all(f.file == "changed.php" for f in findings), [f.file for f in findings]
        assert not any("OUTSIDE-SECRET-MARKER" in f.finding for f in findings)


def _write_hostile_workspace(workspace: Path, *, shadow_main: bool) -> None:
    """Write files that shadow the engine entrypoint and its imports.

    Each file records that it ran by writing a marker into HOSTILE_MARKER_DIR, then
    exits, so a run that imports one of them never reaches the real engine. With
    shadow_main the entrypoint is shadowed and runs first, so the third-party shadows
    are only reached when shadow_main is False.
    """
    body = (
        "import os, pathlib\n"
        "pathlib.Path(os.environ['HOSTILE_MARKER_DIR'], {name!r}).write_text('ran')\n"
        "raise SystemExit(0)\n"
    )
    if shadow_main:
        package = workspace / "ai_pr_review"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(body.format(name="ai_pr_review-main"), encoding="utf-8")
    else:
        workspace.mkdir(parents=True)
    (workspace / "yaml.py").write_text(body.format(name="yaml"), encoding="utf-8")
    (workspace / "httpx.py").write_text(body.format(name="httpx"), encoding="utf-8")
    (workspace / "click").mkdir()
    (workspace / "click" / "__init__.py").write_text(body.format(name="click"), encoding="utf-8")


def _run_entrypoint(workspace: Path, marker_dir: Path, *, safe_path: bool) -> subprocess.CompletedProcess[str]:
    """Start the real `python -m ai_pr_review` with the workspace as the working directory."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONSAFEPATH", "PYTHONPATH"}}
    env["HOSTILE_MARKER_DIR"] = str(marker_dir)
    # The installed package must stay importable. PYTHONPATH entries are not affected by
    # PYTHONSAFEPATH, and the hostile workspace comes first on sys.path only without it.
    env["PYTHONPATH"] = str(REPO_ROOT)
    if safe_path:
        env["PYTHONSAFEPATH"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "ai_pr_review", "--help"],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


@pytest.mark.security
class TestEntrypointIgnoresWorkspaceModules:
    """The image starts in the PR's tree, so `python -m` must not import modules from it."""

    @pytest.mark.parametrize("shadow_main", [True, False], ids=["entrypoint-shadow", "third-party-shadow"])
    def test_hostile_workspace_does_not_run_with_safe_path(self, tmp_path: Path, shadow_main: bool) -> None:
        workspace = tmp_path / "workspace"
        marker_dir = tmp_path / "markers"
        marker_dir.mkdir()
        _write_hostile_workspace(workspace, shadow_main=shadow_main)

        # Control: without the setting a hostile file runs. If this stops running,
        # the main assertion below proves nothing.
        control = _run_entrypoint(workspace, marker_dir, safe_path=False)
        control_ran = sorted(p.name for p in marker_dir.iterdir())
        assert control_ran, (
            "control failed: no hostile file ran, so this test proves nothing "
            f"(rc={control.returncode}, stderr={control.stderr[:300]!r})"
        )
        if shadow_main:
            assert "ai_pr_review-main" in control_ran
        for marker in marker_dir.iterdir():
            marker.unlink()

        result = _run_entrypoint(workspace, marker_dir, safe_path=True)
        ran = sorted(p.name for p in marker_dir.iterdir())
        assert not ran, (
            f"SECURITY REGRESSION: workspace modules ran at start-up: {ran}. "
            "See SECURITY.md and CONTRIBUTING.md for the reporting process."
        )
        assert result.returncode == 0, result.stderr[:500]
        assert "Usage:" in result.stdout, "the real engine should have printed its help text"

    def test_dockerfile_sets_safe_path_in_the_final_stage(self) -> None:
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        final_stage = text[text.rfind("\nFROM "):]
        values = re.findall(r"(?m)^ENV PYTHONSAFEPATH=(\S*)[ \t]*$", final_stage)
        assert values == ["1"], (
            f"the final stage must set ENV PYTHONSAFEPATH=1 exactly once and never override it (found {values})"
        )
        env_at = re.search(r"(?m)^ENV PYTHONSAFEPATH=1[ \t]*$", final_stage)
        entry_at = re.search(r"(?m)^ENTRYPOINT \[", final_stage)
        assert env_at and entry_at and env_at.start() < entry_at.start(), (
            "ENV PYTHONSAFEPATH=1 must come before the ENTRYPOINT in the final stage"
        )

    def test_container_action_passes_safe_path_to_docker_run(self) -> None:
        text = (REPO_ROOT / "container-action" / "action.yml").read_text(encoding="utf-8")
        start = text.find("docker run --rm \\")
        end = text.find('"$IMAGE"', start)
        assert start != -1 and end != -1, "could not find the docker run block"
        block = text[start:end]
        assert re.search(r"(?m)^\s*-e PYTHONSAFEPATH=1 \\$", block), "docker run must pass -e PYTHONSAFEPATH=1"
        assert re.findall(r"PYTHONSAFEPATH=(\S*)", block) == ["1"], "PYTHONSAFEPATH must be set once, to 1"

    def test_root_action_sets_safe_path_on_the_run_step(self) -> None:
        data = yaml.safe_load((REPO_ROOT / "action.yml").read_text(encoding="utf-8"))
        steps = [s for s in data["runs"]["steps"] if s.get("name") == "Run AI review"]
        assert len(steps) == 1, "expected exactly one 'Run AI review' step"
        step = steps[0]
        assert "python3 -m ai_pr_review review" in step["run"]
        assert step["env"].get("PYTHONSAFEPATH") == "1"
        assert "PYTHONSAFEPATH" not in step["run"], "the run script must not unset or change PYTHONSAFEPATH"
